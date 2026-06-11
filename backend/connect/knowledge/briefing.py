"""The Today brief — deterministic SQL over already-enriched rows; ZERO LLM.

Generated lazily on the first GET of the day, persisted as brief +
brief_item rows so it is stable all day, browsable for past dates, and
items can be marked seen. Every item's reason string is template-rendered
from score components at generation time (components are kept in
reason_json for auditability) — NEVER LLM-written.

Phase 2 fills watch_dev and thread_move; Phase 3 (verification slice)
fills contradiction / trending_claim / suggestion. Suggestion scoring is
the deterministic component sum from result.consumption:

    score = 2*w_contradiction + 1*w_trending + 2*w_watch
          + 1*w_calendar + 1*w_official_gap

with the "because..." line template-rendered from the fired components.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta
from typing import Any, Mapping

import psycopg

from connect.domain.enums import BRIEF_SECTIONS
from connect.domain.models import (
    BriefInfo,
    BriefItem,
    BriefResponse,
    BriefSections,
)
from connect.llm.spend import today_utc
from connect.storage import watches as watch_dao
from connect.storage.pg import Jsonb, utc_now

log = logging.getLogger(__name__)

MAX_ITEMS_PER_WATCH = 5
MAX_THREAD_ITEMS = 10
THREAD_LOOKBACK_DAYS = 1  # "moved since yesterday"

# Phase 3 sections ------------------------------------------------------------------
MAX_CONTRADICTION_ITEMS = 10
MAX_TRENDING_ITEMS = 10
MAX_SUGGESTIONS = 5
# v9 -------------------------------------------------------------------------------
MAX_POSITION_SHIFT_ITEMS = 10
# ">= 3 distinct sources" assumes ~15 live sources; start at 2 (design
# result.consumption open question #4)
TRENDING_MIN_SOURCES = 2
TRENDING_WINDOW_DAYS = 1       # sightings since yesterday ~ the 48h window
CALENDAR_WINDOW_DAYS = 90

# suggestion score components (deterministic; design result.consumption §1)
SCORE_CONTRADICTION = 2
SCORE_TRENDING = 1
SCORE_WATCH = 2
SCORE_CALENDAR = 1
SCORE_OFFICIAL_GAP = 1


def _days_before(day_iso: str, days: int) -> str:
    """'YYYY-MM-DD' minus N days — replaces SQLite's date(?, '-N day')."""
    return (_date.fromisoformat(day_iso[:10]) - timedelta(days=days)
            ).isoformat()


@dataclass
class _Draft:
    """One brief item before persistence."""
    section: str
    object_type: str
    object_id: int
    reason: str
    components: dict[str, Any]
    payload: dict[str, Any] = field(default_factory=dict)


# -- public API ----------------------------------------------------------------------

async def get_or_generate(conn: psycopg.AsyncConnection,
                          brief_date: str | None = None,
                          ) -> BriefResponse | None:
    """Today's brief (generating it on first call); a past date returns its
    persisted brief or None (history is never back-filled)."""
    today = today_utc()
    date = brief_date or today
    row = await _brief_row(conn, date)
    if row is None:
        if date != today:
            return None
        await _generate(conn, date)
        row = await _brief_row(conn, date)
        assert row is not None
    return await _assemble(conn, row)


async def mark_seen(conn: psycopg.AsyncConnection, item_id: int) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE brief_item SET seen = TRUE WHERE id = %s", (item_id,))
    return cur.rowcount > 0


# -- generation -----------------------------------------------------------------------

async def _generate(conn: psycopg.AsyncConnection, brief_date: str) -> None:
    """Compute and persist one day's brief in a single transaction.
    Idempotent under races: brief.brief_date is UNIQUE (NULLS NOT DISTINCT
    with the single-user NULL user_id); the loser no-ops."""
    drafts: list[_Draft] = []
    drafts += await _watch_dev_items(conn)
    drafts += await _thread_move_items(conn, brief_date)
    drafts += await _contradiction_items(conn, brief_date)
    drafts += await _trending_claim_items(conn, brief_date)
    drafts += await _suggestion_items(conn, brief_date)
    drafts += await _position_shift_items(conn, brief_date)
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO brief (brief_date, generated_at) VALUES (%s, %s)"
            " ON CONFLICT (user_id, brief_date) DO NOTHING RETURNING id",
            (brief_date, utc_now()))
        row = await cur.fetchone()
        if row is None:  # concurrent first-GET already generated it
            return
        brief_id = int(row["id"])
        ranks = {section: 0 for section in BRIEF_SECTIONS}
        for draft in drafts:
            ranks[draft.section] += 1
            await conn.execute(
                "INSERT INTO brief_item (brief_id, section, rank,"
                " object_type, object_id, reason_json, payload, seen)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE)",
                (brief_id, draft.section, ranks[draft.section],
                 draft.object_type, draft.object_id,
                 Jsonb({"reason": draft.reason,
                        "components": draft.components}),
                 Jsonb(draft.payload)))


async def _watch_dev_items(conn: psycopg.AsyncConnection) -> list[_Draft]:
    """Per-watch new hits since the watch's read cursor — the 'On your
    watches' section. One item per hit object, newest first, capped per
    watch; watches in id order."""
    drafts: list[_Draft] = []
    for watch in await watch_dao.list_active(conn):
        cursor = watch.last_seen_at or "1970-01-01"
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM watch_hit"
            " WHERE watch_id = %s AND created_at > %s",
            (watch.id, cursor))
        total = (await cur.fetchone())["n"]
        if not total:
            continue
        cur = await conn.execute(
            "SELECT object_type, object_id, created_at FROM watch_hit"
            " WHERE watch_id = %s AND created_at > %s"
            " ORDER BY created_at DESC, object_id DESC LIMIT %s",
            (watch.id, cursor, MAX_ITEMS_PER_WATCH))
        hits = await cur.fetchall()
        reason = (f"Matched watch '{watch.label}'"
                  f" · {total} new since last seen")
        components = {"watch_id": watch.id, "watch_label": watch.label,
                      "new_count": int(total)}
        for hit in hits:
            payload = {"watch_id": watch.id, "watch_label": watch.label,
                       **await _object_payload(conn, hit["object_type"],
                                               hit["object_id"])}
            drafts.append(_Draft(
                section="watch_dev", object_type=hit["object_type"],
                object_id=hit["object_id"], reason=reason,
                components=components, payload=payload))
    return drafts


async def _object_payload(conn: psycopg.AsyncConnection, object_type: str,
                          object_id: int) -> dict[str, Any]:
    """Denormalized display fields for one brief object (frozen at
    generation so the brief stays stable all day)."""
    if object_type == "document":
        cur = await conn.execute(
            "SELECT d.title, COALESCE(d.published_at, d.fetched_at) AS day,"
            " s.name AS source_name FROM document d"
            " LEFT JOIN source s ON s.id = d.source_id WHERE d.id = %s",
            (object_id,))
        row = await cur.fetchone()
        if row is not None:
            return {"title": row["title"], "date": row["day"],
                    "source": row["source_name"]}
    elif object_type == "event":
        cur = await conn.execute(
            "SELECT title, event_type, occurred_on, doc_count FROM event"
            " WHERE id = %s", (object_id,))
        row = await cur.fetchone()
        if row is not None:
            return {"title": row["title"], "event_type": row["event_type"],
                    "date": row["occurred_on"],
                    "doc_count": row["doc_count"]}
    elif object_type == "claim":
        cur = await conn.execute(
            "SELECT text, verdict FROM claim WHERE id = %s",
            (object_id,))
        row = await cur.fetchone()
        if row is not None:
            return {"title": row["text"], "verdict": row["verdict"]}
    return {}


async def _thread_move_items(conn: psycopg.AsyncConnection,
                             brief_date: str) -> list[_Draft]:
    """Stories with new member events since yesterday, ordered by heat =
    count of distinct credibility-tier<=2 sources behind the new events."""
    since = _days_before(brief_date, THREAD_LOOKBACK_DAYS)
    cur = await conn.execute(
        "SELECT s.id, s.title, s.doc_count, s.updated_at,"
        " COUNT(DISTINCT ev.id) AS new_events"
        " FROM story s JOIN event ev ON ev.story_id = s.id"
        " WHERE ev.created_at >= %s AND s.status = 'active'"
        " GROUP BY s.id, s.title, s.doc_count, s.updated_at", (since,))
    stories = await cur.fetchall()
    drafts: list[tuple[int, int, _Draft]] = []
    for story in stories:
        cur = await conn.execute(
            "SELECT COUNT(DISTINCT d.source_id) AS n"
            " FROM event_assignment ea"
            " JOIN event ev ON ev.id = ea.event_id"
            " JOIN document d ON d.id = ea.document_id"
            " JOIN source src ON src.id = d.source_id"
            " WHERE ev.story_id = %s AND ev.created_at >= %s"
            " AND src.credibility_tier <= 2",
            (story["id"], since))
        heat = (await cur.fetchone())["n"]
        n = int(story["new_events"])
        plural = "s" if n != 1 else ""
        reason = (f"+{n} new event{plural}"
                  f" · {heat} tier-1/2 source{'s' if heat != 1 else ''}")
        drafts.append((int(heat), n, _Draft(
            section="thread_move", object_type="thread",
            object_id=story["id"], reason=reason,
            components={"new_events": n, "heat": int(heat)},
            payload={"title": story["title"], "new_events": n,
                     "heat": int(heat), "doc_count": story["doc_count"],
                     "updated_at": story["updated_at"]})))
    drafts.sort(key=lambda t: (-t[0], -t[1], t[2].object_id))
    return [d for _, _, d in drafts[:MAX_THREAD_ITEMS]]


async def _contradiction_items(conn: psycopg.AsyncConnection,
                               brief_date: str) -> list[_Draft]:
    """Open contradictions detected since yesterday (the materialized
    ledger; the scanner runs with the verify stage)."""
    since = _days_before(brief_date, 1)
    cur = await conn.execute(
        "SELECT k.id, k.claim_id, c.text, c.verdict, k.n_support,"
        " k.n_refute, k.best_tier_support, k.best_tier_refute"
        " FROM contradiction k JOIN claim c ON c.id = k.claim_id"
        " WHERE k.status = 'open' AND k.detected_at >= %s"
        " ORDER BY k.detected_at DESC, k.id DESC LIMIT %s",
        (since, MAX_CONTRADICTION_ITEMS))
    rows = await cur.fetchall()
    drafts: list[_Draft] = []
    for row in rows:
        t_s = row["best_tier_support"] or "?"
        t_r = row["best_tier_refute"] or "?"
        reason = (f"Disputed: {row['n_support']} support vs"
                  f" {row['n_refute']} refute · best tiers {t_s}/{t_r}")
        drafts.append(_Draft(
            section="contradiction", object_type="contradiction",
            object_id=row["id"], reason=reason,
            components={"n_support": row["n_support"],
                        "n_refute": row["n_refute"],
                        "best_tier_support": row["best_tier_support"],
                        "best_tier_refute": row["best_tier_refute"]},
            payload={"title": row["text"], "claim_id": row["claim_id"],
                     "verdict": row["verdict"],
                     "n_support": row["n_support"],
                     "n_refute": row["n_refute"],
                     "best_tier_support": row["best_tier_support"],
                     "best_tier_refute": row["best_tier_refute"]}))
    return drafts


async def _trending_claim_items(conn: psycopg.AsyncConnection,
                                brief_date: str) -> list[_Draft]:
    """Claims sighted by >= TRENDING_MIN_SOURCES distinct sources within
    the 48h window, with a per-tier breakdown in the payload."""
    since = _days_before(brief_date, TRENDING_WINDOW_DAYS)
    cur = await conn.execute(
        "SELECT cs.claim_id, c.text, c.verdict,"
        " COUNT(DISTINCT d.source_id) AS n_sources"
        " FROM claim_sighting cs"
        " JOIN document d ON d.id = cs.document_id"
        " JOIN claim c ON c.id = cs.claim_id"
        " WHERE cs.created_at >= %s AND d.source_id IS NOT NULL"
        " GROUP BY cs.claim_id, c.text, c.verdict"
        " HAVING COUNT(DISTINCT d.source_id) >= %s"
        " ORDER BY n_sources DESC, cs.claim_id ASC LIMIT %s",
        (since, TRENDING_MIN_SOURCES, MAX_TRENDING_ITEMS))
    rows = await cur.fetchall()
    drafts: list[_Draft] = []
    for row in rows:
        cur = await conn.execute(
            "SELECT s.credibility_tier, COUNT(DISTINCT s.id) AS n"
            " FROM claim_sighting cs"
            " JOIN document d ON d.id = cs.document_id"
            " JOIN source s ON s.id = d.source_id"
            " WHERE cs.claim_id = %s AND cs.created_at >= %s"
            " GROUP BY s.credibility_tier",
            (row["claim_id"], since))
        tiers = {str(t["credibility_tier"]): int(t["n"])
                 for t in await cur.fetchall()}
        n = int(row["n_sources"])
        reason = f"{n} sources in 48h"
        drafts.append(_Draft(
            section="trending_claim", object_type="claim",
            object_id=row["claim_id"], reason=reason,
            components={"sources_48h": n, "tiers": tiers},
            payload={"title": row["text"], "verdict": row["verdict"],
                     "sources_48h": n, "tiers": tiers}))
    return drafts


async def _suggestion_items(conn: psycopg.AsyncConnection,
                            brief_date: str) -> list[_Draft]:
    """Top-5 suggested analyses by the deterministic component score;
    reasons are template-rendered from the fired components — NEVER LLM,
    auditable enough to justify spending money on an analysis."""
    since = _days_before(brief_date, TRENDING_WINDOW_DAYS)
    cur = await conn.execute(
        "SELECT DISTINCT claim_id FROM ("
        " SELECT claim_id FROM claim_sighting WHERE created_at >= %s"
        " UNION SELECT claim_id FROM contradiction WHERE status = 'open')"
        " AS candidates",
        (since,))
    candidates = [r["claim_id"] for r in await cur.fetchall()]
    scored: list[tuple[int, int, _Draft]] = []
    for claim_id in candidates:
        draft = await _score_suggestion(conn, brief_date, claim_id)
        if draft is not None:
            scored.append((int(draft.components["score"]), claim_id, draft))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [d for _, _, d in scored[:MAX_SUGGESTIONS]]


async def _claim_document_ids(conn: psycopg.AsyncConnection,
                              claim_id: int) -> list[int]:
    """Documents linked to the claim via sightings (enrichment-grade) or
    evidence (analysis-grade)."""
    cur = await conn.execute(
        "SELECT document_id FROM claim_sighting WHERE claim_id = %s"
        " UNION SELECT document_id FROM evidence WHERE claim_id = %s",
        (claim_id, claim_id))
    return [r["document_id"] for r in await cur.fetchall()]


async def _score_suggestion(conn: psycopg.AsyncConnection, brief_date: str,
                            claim_id: int) -> _Draft | None:
    cur = await conn.execute(
        "SELECT id, text, verdict FROM claim WHERE id = %s", (claim_id,))
    claim = await cur.fetchone()
    if claim is None:
        return None
    doc_ids = await _claim_document_ids(conn, claim_id)
    since = _days_before(brief_date, TRENDING_WINDOW_DAYS)
    score = 0
    parts: list[str] = []
    components: dict[str, Any] = {}

    cur = await conn.execute(
        "SELECT n_support, n_refute FROM contradiction"
        " WHERE claim_id = %s AND status = 'open'", (claim_id,))
    contradiction = await cur.fetchone()
    if contradiction is not None:
        score += SCORE_CONTRADICTION
        components["contradiction"] = {
            "n_support": contradiction["n_support"],
            "n_refute": contradiction["n_refute"]}
        parts.append(f"Disputed across sources"
                     f" ({contradiction['n_support']} support vs"
                     f" {contradiction['n_refute']} refute)")

    cur = await conn.execute(
        "SELECT COUNT(DISTINCT d.source_id) AS n FROM claim_sighting cs"
        " JOIN document d ON d.id = cs.document_id"
        " WHERE cs.claim_id = %s AND cs.created_at >= %s"
        " AND d.source_id IS NOT NULL",
        (claim_id, since))
    n_sources = (await cur.fetchone())["n"]
    if int(n_sources) >= TRENDING_MIN_SOURCES:
        score += SCORE_TRENDING
        components["trending"] = {"sources_48h": int(n_sources)}
        parts.append(f"{n_sources} sources in 48h")

    watch_label = None
    if doc_ids:
        cur = await conn.execute(
            "SELECT w.label FROM watch w"
            " JOIN entity_mention m ON m.entity_id = w.entity_id"
            " WHERE w.kind = 'entity' AND NOT w.muted"
            " AND m.document_id = ANY(%s)"
            " ORDER BY w.id LIMIT 1", (doc_ids,))
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute(
                "SELECT w.label FROM watch w"
                " JOIN watch_hit h ON h.watch_id = w.id"
                " WHERE NOT w.muted AND h.object_type = 'document'"
                " AND h.object_id = ANY(%s)"
                " ORDER BY w.id LIMIT 1", (doc_ids,))
            row = await cur.fetchone()
        watch_label = row["label"] if row is not None else None
    if watch_label is not None:
        score += SCORE_WATCH
        components["watch"] = {"label": watch_label}
        parts.append(f"touches watch: {watch_label}")

    if doc_ids:
        cur = await conn.execute(
            "SELECT ce.label,"
            " (ce.occurs_on - ev.occurred_on) AS days_before"
            " FROM event ev"
            " JOIN event_assignment ea ON ea.event_id = ev.id"
            " JOIN calendar_event ce"
            "   ON (ce.occurs_on - ev.occurred_on) BETWEEN 0 AND %s"
            " WHERE ea.document_id = ANY(%s)"
            " AND ev.occurred_on IS NOT NULL"
            " ORDER BY days_before LIMIT 1",
            (CALENDAR_WINDOW_DAYS, doc_ids))
        cal = await cur.fetchone()
        if cal is not None:
            score += SCORE_CALENDAR
            components["calendar"] = {"label": cal["label"],
                                      "days_before": cal["days_before"]}
            parts.append(f"{cal['days_before']}d before {cal['label']}")

    if doc_ids:
        cur = await conn.execute(
            "SELECT 1 FROM document d JOIN source s ON s.id = d.source_id"
            " WHERE d.id = ANY(%s) AND s.credibility_tier = 1 LIMIT 1",
            (doc_ids,))
        has_tier1 = await cur.fetchone() is not None
        if not has_tier1:
            score += SCORE_OFFICIAL_GAP
            components["official_gap"] = True
            parts.append("no tier-1 source yet")

    if score <= 0:
        return None
    components["score"] = score
    return _Draft(
        section="suggestion", object_type="claim", object_id=claim_id,
        reason=" · ".join(parts), components=components,
        payload={"title": claim["text"], "verdict": claim["verdict"],
                 "score": score})


async def _position_shift_items(conn: psycopg.AsyncConnection,
                                brief_date: str) -> list[_Draft]:
    """Open position shifts detected since the LAST brief (all open shifts
    on the very first brief). Watched entities rank first, then newest.
    Both quotes are frozen in the payload — verbatim by construction."""
    cur = await conn.execute(
        "SELECT generated_at FROM brief WHERE brief_date < %s"
        " ORDER BY brief_date DESC LIMIT 1", (brief_date,))
    prev = await cur.fetchone()
    since = prev["generated_at"] if prev is not None else "1970-01-01"
    cur = await conn.execute(
        "SELECT ps.id, ps.entity_id, ps.topic, ps.kind, ps.note,"
        " ps.detected_at, e.name AS entity_name,"
        " sf.quote AS from_quote, st.quote AS to_quote,"
        " sf.stated_at AS from_date, st.stated_at AS to_date,"
        " EXISTS (SELECT 1 FROM watch w WHERE w.kind = 'entity'"
        "   AND w.entity_id = ps.entity_id AND NOT w.muted) AS watched"
        " FROM position_shift ps"
        " JOIN entity e ON e.id = ps.entity_id"
        " JOIN statement sf ON sf.id = ps.from_statement_id"
        " JOIN statement st ON st.id = ps.to_statement_id"
        " WHERE ps.status = 'open' AND ps.detected_at > %s"
        " ORDER BY watched DESC, ps.detected_at DESC, ps.id DESC LIMIT %s",
        (since, MAX_POSITION_SHIFT_ITEMS))
    rows = await cur.fetchall()
    drafts: list[_Draft] = []
    for row in rows:
        from_day = (row["from_date"] or "")[:10] or "earlier"
        to_day = (row["to_date"] or "")[:10] or "now"
        verb = "Reversed" if row["kind"] == "reversed" else "Shifted"
        reason = (f"{verb} position on {row['topic']}"
                  f" · was {from_day}, now {to_day}")
        drafts.append(_Draft(
            section="position_shift", object_type="position_shift",
            object_id=row["id"], reason=reason,
            components={"kind": row["kind"], "topic": row["topic"],
                        "watched": bool(row["watched"])},
            payload={"entity_id": row["entity_id"],
                     "entity_name": row["entity_name"],
                     "topic": row["topic"], "kind": row["kind"],
                     "from_quote": row["from_quote"],
                     "to_quote": row["to_quote"],
                     "from_date": row["from_date"],
                     "to_date": row["to_date"]}))
    return drafts


# -- read path -------------------------------------------------------------------------

async def _brief_row(conn: psycopg.AsyncConnection,
                     date: str) -> Mapping[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, brief_date, generated_at FROM brief"
        " WHERE brief_date = %s", (date,))
    return await cur.fetchone()


async def _assemble(conn: psycopg.AsyncConnection,
                    brief: Mapping[str, Any]) -> BriefResponse:
    sections: dict[str, list[BriefItem]] = {s: [] for s in BRIEF_SECTIONS}
    cur = await conn.execute(
        "SELECT id, section, rank, object_type, object_id, reason_json,"
        " payload, seen FROM brief_item WHERE brief_id = %s"
        " ORDER BY section, rank", (brief["id"],))
    for row in await cur.fetchall():
        reason_json = row["reason_json"] \
            if isinstance(row["reason_json"], dict) else {}
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        sections[row["section"]].append(BriefItem(
            id=row["id"], section=row["section"], rank=row["rank"],
            object_type=row["object_type"], object_id=row["object_id"],
            reason=reason_json.get("reason", ""), payload=payload,
            seen=bool(row["seen"])))
    return BriefResponse(
        brief=BriefInfo(id=brief["id"], brief_date=brief["brief_date"],
                        generated_at=brief["generated_at"]),
        sections=BriefSections(**sections))
