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

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from connect.domain.enums import BRIEF_SECTIONS
from connect.domain.models import (
    BriefInfo,
    BriefItem,
    BriefResponse,
    BriefSections,
)
from connect.llm.spend import today_utc
from connect.storage import watches as watch_dao
from connect.storage.db import utc_now

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
TRENDING_WINDOW = "-1 day"     # sightings since yesterday ~ the 48h window
CALENDAR_WINDOW_DAYS = 90

# suggestion score components (deterministic; design result.consumption §1)
SCORE_CONTRADICTION = 2
SCORE_TRENDING = 1
SCORE_WATCH = 2
SCORE_CALENDAR = 1
SCORE_OFFICIAL_GAP = 1


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

def get_or_generate(conn: sqlite3.Connection,
                    brief_date: str | None = None) -> BriefResponse | None:
    """Today's brief (generating it on first call); a past date returns its
    persisted brief or None (history is never back-filled)."""
    today = today_utc()
    date = brief_date or today
    row = _brief_row(conn, date)
    if row is None:
        if date != today:
            return None
        _generate(conn, date)
        row = _brief_row(conn, date)
        assert row is not None
    return _assemble(conn, row)


def mark_seen(conn: sqlite3.Connection, item_id: int) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE brief_item SET seen = 1 WHERE id = ?", (item_id,))
    return cur.rowcount > 0


# -- generation -----------------------------------------------------------------------

def _generate(conn: sqlite3.Connection, brief_date: str) -> None:
    """Compute and persist one day's brief in a single transaction.
    Idempotent under races: brief.brief_date is UNIQUE; the loser no-ops."""
    drafts: list[_Draft] = []
    drafts += _watch_dev_items(conn)
    drafts += _thread_move_items(conn, brief_date)
    drafts += _contradiction_items(conn, brief_date)
    drafts += _trending_claim_items(conn, brief_date)
    drafts += _suggestion_items(conn, brief_date)
    drafts += _position_shift_items(conn, brief_date)
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO brief (brief_date, generated_at)"
            " VALUES (?, ?)", (brief_date, utc_now()))
        if cur.rowcount == 0:  # concurrent first-GET already generated it
            return
        brief_id = int(cur.lastrowid)  # type: ignore[arg-type]
        ranks = {section: 0 for section in BRIEF_SECTIONS}
        for draft in drafts:
            ranks[draft.section] += 1
            conn.execute(
                "INSERT INTO brief_item (brief_id, section, rank,"
                " object_type, object_id, reason_json, payload, seen)"
                " VALUES (?,?,?,?,?,?,?,0)",
                (brief_id, draft.section, ranks[draft.section],
                 draft.object_type, draft.object_id,
                 json.dumps({"reason": draft.reason,
                             "components": draft.components}),
                 json.dumps(draft.payload)))


def _watch_dev_items(conn: sqlite3.Connection) -> list[_Draft]:
    """Per-watch new hits since the watch's read cursor — the 'On your
    watches' section. One item per hit object, newest first, capped per
    watch; watches in id order."""
    drafts: list[_Draft] = []
    for watch in watch_dao.list_active(conn):
        cursor = watch.last_seen_at or "1970-01-01"
        total = conn.execute(
            "SELECT COUNT(*) FROM watch_hit"
            " WHERE watch_id = ? AND created_at > ?",
            (watch.id, cursor)).fetchone()[0]
        if not total:
            continue
        hits = conn.execute(
            "SELECT object_type, object_id, created_at FROM watch_hit"
            " WHERE watch_id = ? AND created_at > ?"
            " ORDER BY created_at DESC, object_id DESC LIMIT ?",
            (watch.id, cursor, MAX_ITEMS_PER_WATCH)).fetchall()
        reason = (f"Matched watch '{watch.label}'"
                  f" · {total} new since last seen")
        components = {"watch_id": watch.id, "watch_label": watch.label,
                      "new_count": int(total)}
        for hit in hits:
            payload = {"watch_id": watch.id, "watch_label": watch.label,
                       **_object_payload(conn, hit["object_type"],
                                         hit["object_id"])}
            drafts.append(_Draft(
                section="watch_dev", object_type=hit["object_type"],
                object_id=hit["object_id"], reason=reason,
                components=components, payload=payload))
    return drafts


def _object_payload(conn: sqlite3.Connection, object_type: str,
                    object_id: int) -> dict[str, Any]:
    """Denormalized display fields for one brief object (frozen at
    generation so the brief stays stable all day)."""
    if object_type == "document":
        row = conn.execute(
            "SELECT d.title, COALESCE(d.published_at, d.fetched_at) AS day,"
            " s.name AS source_name FROM document d"
            " LEFT JOIN source s ON s.id = d.source_id WHERE d.id = ?",
            (object_id,)).fetchone()
        if row is not None:
            return {"title": row["title"], "date": row["day"],
                    "source": row["source_name"]}
    elif object_type == "event":
        row = conn.execute(
            "SELECT title, event_type, occurred_on, doc_count FROM event"
            " WHERE id = ?", (object_id,)).fetchone()
        if row is not None:
            return {"title": row["title"], "event_type": row["event_type"],
                    "date": row["occurred_on"],
                    "doc_count": row["doc_count"]}
    elif object_type == "claim":
        row = conn.execute(
            "SELECT text, verdict FROM claim WHERE id = ?",
            (object_id,)).fetchone()
        if row is not None:
            return {"title": row["text"], "verdict": row["verdict"]}
    return {}


def _thread_move_items(conn: sqlite3.Connection,
                       brief_date: str) -> list[_Draft]:
    """Stories with new member events since yesterday, ordered by heat =
    count of distinct credibility-tier<=2 sources behind the new events."""
    since = conn.execute(
        "SELECT date(?, ?)",
        (brief_date, f"-{THREAD_LOOKBACK_DAYS} day")).fetchone()[0]
    stories = conn.execute(
        "SELECT s.id, s.title, s.doc_count, s.updated_at,"
        " COUNT(DISTINCT ev.id) AS new_events"
        " FROM story s JOIN event ev ON ev.story_id = s.id"
        " WHERE ev.created_at >= ? AND s.status = 'active'"
        " GROUP BY s.id", (since,)).fetchall()
    drafts: list[tuple[int, int, _Draft]] = []
    for story in stories:
        heat = conn.execute(
            "SELECT COUNT(DISTINCT d.source_id) FROM event_assignment ea"
            " JOIN event ev ON ev.id = ea.event_id"
            " JOIN document d ON d.id = ea.document_id"
            " JOIN source src ON src.id = d.source_id"
            " WHERE ev.story_id = ? AND ev.created_at >= ?"
            " AND src.credibility_tier <= 2",
            (story["id"], since)).fetchone()[0]
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


def _contradiction_items(conn: sqlite3.Connection,
                         brief_date: str) -> list[_Draft]:
    """Open contradictions detected since yesterday (the materialized
    ledger; the scanner runs with the verify stage)."""
    since = conn.execute(
        "SELECT date(?, '-1 day')", (brief_date,)).fetchone()[0]
    rows = conn.execute(
        "SELECT k.id, k.claim_id, c.text, c.verdict, k.n_support,"
        " k.n_refute, k.best_tier_support, k.best_tier_refute"
        " FROM contradiction k JOIN claim c ON c.id = k.claim_id"
        " WHERE k.status = 'open' AND k.detected_at >= ?"
        " ORDER BY k.detected_at DESC, k.id DESC LIMIT ?",
        (since, MAX_CONTRADICTION_ITEMS)).fetchall()
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


def _trending_claim_items(conn: sqlite3.Connection,
                          brief_date: str) -> list[_Draft]:
    """Claims sighted by >= TRENDING_MIN_SOURCES distinct sources within
    the 48h window, with a per-tier breakdown in the payload."""
    rows = conn.execute(
        "SELECT cs.claim_id, c.text, c.verdict,"
        " COUNT(DISTINCT d.source_id) AS n_sources"
        " FROM claim_sighting cs"
        " JOIN document d ON d.id = cs.document_id"
        " JOIN claim c ON c.id = cs.claim_id"
        " WHERE cs.created_at >= datetime(?, ?) AND d.source_id IS NOT NULL"
        " GROUP BY cs.claim_id HAVING n_sources >= ?"
        " ORDER BY n_sources DESC, cs.claim_id ASC LIMIT ?",
        (brief_date, TRENDING_WINDOW, TRENDING_MIN_SOURCES,
         MAX_TRENDING_ITEMS)).fetchall()
    drafts: list[_Draft] = []
    for row in rows:
        tiers = {
            str(t["credibility_tier"]): int(t["n"])
            for t in conn.execute(
                "SELECT s.credibility_tier, COUNT(DISTINCT s.id) AS n"
                " FROM claim_sighting cs"
                " JOIN document d ON d.id = cs.document_id"
                " JOIN source s ON s.id = d.source_id"
                " WHERE cs.claim_id = ? AND cs.created_at >= datetime(?, ?)"
                " GROUP BY s.credibility_tier",
                (row["claim_id"], brief_date, TRENDING_WINDOW))}
        n = int(row["n_sources"])
        reason = f"{n} sources in 48h"
        drafts.append(_Draft(
            section="trending_claim", object_type="claim",
            object_id=row["claim_id"], reason=reason,
            components={"sources_48h": n, "tiers": tiers},
            payload={"title": row["text"], "verdict": row["verdict"],
                     "sources_48h": n, "tiers": tiers}))
    return drafts


def _suggestion_items(conn: sqlite3.Connection,
                      brief_date: str) -> list[_Draft]:
    """Top-5 suggested analyses by the deterministic component score;
    reasons are template-rendered from the fired components — NEVER LLM,
    auditable enough to justify spending money on an analysis."""
    candidates = [r[0] for r in conn.execute(
        "SELECT DISTINCT claim_id FROM ("
        " SELECT claim_id FROM claim_sighting"
        "  WHERE created_at >= datetime(?, ?)"
        " UNION SELECT claim_id FROM contradiction WHERE status = 'open')",
        (brief_date, TRENDING_WINDOW))]
    scored: list[tuple[int, int, _Draft]] = []
    for claim_id in candidates:
        draft = _score_suggestion(conn, brief_date, claim_id)
        if draft is not None:
            scored.append((int(draft.components["score"]), claim_id, draft))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [d for _, _, d in scored[:MAX_SUGGESTIONS]]


def _claim_document_ids(conn: sqlite3.Connection,
                        claim_id: int) -> list[int]:
    """Documents linked to the claim via sightings (enrichment-grade) or
    evidence (analysis-grade)."""
    return [r[0] for r in conn.execute(
        "SELECT document_id FROM claim_sighting WHERE claim_id = ?"
        " UNION SELECT document_id FROM evidence WHERE claim_id = ?",
        (claim_id, claim_id))]


def _score_suggestion(conn: sqlite3.Connection, brief_date: str,
                      claim_id: int) -> _Draft | None:
    claim = conn.execute(
        "SELECT id, text, verdict FROM claim WHERE id = ?",
        (claim_id,)).fetchone()
    if claim is None:
        return None
    doc_ids = _claim_document_ids(conn, claim_id)
    marks = ",".join("?" * len(doc_ids))
    score = 0
    parts: list[str] = []
    components: dict[str, Any] = {}

    contradiction = conn.execute(
        "SELECT n_support, n_refute FROM contradiction"
        " WHERE claim_id = ? AND status = 'open'", (claim_id,)).fetchone()
    if contradiction is not None:
        score += SCORE_CONTRADICTION
        components["contradiction"] = {
            "n_support": contradiction["n_support"],
            "n_refute": contradiction["n_refute"]}
        parts.append(f"Disputed across sources"
                     f" ({contradiction['n_support']} support vs"
                     f" {contradiction['n_refute']} refute)")

    n_sources = conn.execute(
        "SELECT COUNT(DISTINCT d.source_id) FROM claim_sighting cs"
        " JOIN document d ON d.id = cs.document_id"
        " WHERE cs.claim_id = ? AND cs.created_at >= datetime(?, ?)"
        " AND d.source_id IS NOT NULL",
        (claim_id, brief_date, TRENDING_WINDOW)).fetchone()[0]
    if int(n_sources) >= TRENDING_MIN_SOURCES:
        score += SCORE_TRENDING
        components["trending"] = {"sources_48h": int(n_sources)}
        parts.append(f"{n_sources} sources in 48h")

    watch_label = None
    if doc_ids:
        row = conn.execute(
            f"SELECT w.label FROM watch w"
            f" JOIN entity_mention m ON m.entity_id = w.entity_id"
            f" WHERE w.kind = 'entity' AND w.muted = 0"
            f" AND m.document_id IN ({marks})"
            f" ORDER BY w.id LIMIT 1", doc_ids).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT w.label FROM watch w"
                f" JOIN watch_hit h ON h.watch_id = w.id"
                f" WHERE w.muted = 0 AND h.object_type = 'document'"
                f" AND h.object_id IN ({marks})"
                f" ORDER BY w.id LIMIT 1", doc_ids).fetchone()
        watch_label = row["label"] if row is not None else None
    if watch_label is not None:
        score += SCORE_WATCH
        components["watch"] = {"label": watch_label}
        parts.append(f"touches watch: {watch_label}")

    if doc_ids:
        cal = conn.execute(
            f"SELECT ce.label, CAST(julianday(ce.occurs_on)"
            f" - julianday(ev.occurred_on) AS INT) AS days_before"
            f" FROM event ev"
            f" JOIN event_assignment ea ON ea.event_id = ev.id"
            f" JOIN calendar_event ce"
            f"   ON julianday(ce.occurs_on) - julianday(ev.occurred_on)"
            f"      BETWEEN 0 AND {CALENDAR_WINDOW_DAYS}"
            f" WHERE ea.document_id IN ({marks})"
            f" AND ev.occurred_on IS NOT NULL"
            f" ORDER BY days_before LIMIT 1", doc_ids).fetchone()
        if cal is not None:
            score += SCORE_CALENDAR
            components["calendar"] = {"label": cal["label"],
                                      "days_before": cal["days_before"]}
            parts.append(f"{cal['days_before']}d before {cal['label']}")

    if doc_ids:
        has_tier1 = conn.execute(
            f"SELECT 1 FROM document d JOIN source s ON s.id = d.source_id"
            f" WHERE d.id IN ({marks}) AND s.credibility_tier = 1 LIMIT 1",
            doc_ids).fetchone() is not None
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


def _position_shift_items(conn: sqlite3.Connection,
                          brief_date: str) -> list[_Draft]:
    """Open position shifts detected since the LAST brief (all open shifts
    on the very first brief). Watched entities rank first, then newest.
    Both quotes are frozen in the payload — verbatim by construction."""
    prev = conn.execute(
        "SELECT generated_at FROM brief WHERE brief_date < ?"
        " ORDER BY brief_date DESC LIMIT 1", (brief_date,)).fetchone()
    since = prev["generated_at"] if prev is not None else "1970-01-01"
    rows = conn.execute(
        "SELECT ps.id, ps.entity_id, ps.topic, ps.kind, ps.note,"
        " ps.detected_at, e.name AS entity_name,"
        " sf.quote AS from_quote, st.quote AS to_quote,"
        " sf.stated_at AS from_date, st.stated_at AS to_date,"
        " EXISTS (SELECT 1 FROM watch w WHERE w.kind = 'entity'"
        "   AND w.entity_id = ps.entity_id AND w.muted = 0) AS watched"
        " FROM position_shift ps"
        " JOIN entity e ON e.id = ps.entity_id"
        " JOIN statement sf ON sf.id = ps.from_statement_id"
        " JOIN statement st ON st.id = ps.to_statement_id"
        " WHERE ps.status = 'open' AND ps.detected_at > ?"
        " ORDER BY watched DESC, ps.detected_at DESC, ps.id DESC LIMIT ?",
        (since, MAX_POSITION_SHIFT_ITEMS)).fetchall()
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

def _brief_row(conn: sqlite3.Connection, date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, brief_date, generated_at FROM brief WHERE brief_date = ?",
        (date,)).fetchone()


def _assemble(conn: sqlite3.Connection, brief: sqlite3.Row) -> BriefResponse:
    sections: dict[str, list[BriefItem]] = {s: [] for s in BRIEF_SECTIONS}
    for row in conn.execute(
            "SELECT id, section, rank, object_type, object_id, reason_json,"
            " payload, seen FROM brief_item WHERE brief_id = ?"
            " ORDER BY section, rank", (brief["id"],)):
        try:
            reason = json.loads(row["reason_json"] or "{}").get("reason", "")
        except (ValueError, TypeError):
            reason = ""
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        sections[row["section"]].append(BriefItem(
            id=row["id"], section=row["section"], rank=row["rank"],
            object_type=row["object_type"], object_id=row["object_id"],
            reason=reason, payload=payload, seen=bool(row["seen"])))
    return BriefResponse(
        brief=BriefInfo(id=brief["id"], brief_date=brief["brief_date"],
                        generated_at=brief["generated_at"]),
        sections=BriefSections(**sections))
