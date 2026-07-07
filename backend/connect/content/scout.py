"""The topic scout — how the reel factory finds its subjects in the feed.

Two halves, model proposes / code disposes like the editorial planner:

1. ``scout_candidates`` — deterministic SQL over the last N hours, no LLM.
   Candidates come from the three tiers of corpus structure, best first:
   story THREADS with fresh events (the strongest subjects — clustered,
   titled, multi-event), fresh EVENT clusters not yet threaded, and hot
   TOPIC tags as a fallback when the knowledge layer is thin. Heat is the
   established briefing metric: distinct corroborating outlets, weighted
   toward credibility tier 1-2. Subjects covered by a recent campaign are
   dropped (the factory never re-makes yesterday's reel).

2. ``pick_assignments`` — one FAST structured call: an assignment editor
   reads the candidate slate (signals included) and commissions the K most
   REEL-worthy, each with a one-line angle. Code enforces that every pick
   maps back to a real candidate (by index), bounds all free text, and
   degrades to the top-of-slate ordering when the LLM is unavailable —
   a scout failure must never kill a factory run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from connect.analysis.budget import AnalysisBudget, AnalysisBudgetExceeded
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded
from connect.llm.tiers import ModelTier

log = logging.getLogger(__name__)

PURPOSE = "reel_scout"
PICK_MAX_TOKENS = 900
EST_IN, EST_OUT = 2500, 300

MAX_CANDIDATES = 24          # slate size handed to the assignment editor
MAX_ANGLE = 200
_MARKER_RE = re.compile(r"\[+\s*E\d+\s*\]+|\bE\d+\b")


@dataclass(frozen=True)
class ScoutCandidate:
    """One potential reel subject, with the signals that surfaced it."""
    subject: str                       # what the campaign will be about
    kind: str                          # 'thread' | 'event' | 'topic'
    story_id: int | None = None       # set for kind == 'thread'
    heat: int = 0                     # distinct tier<=2 outlets in window
    doc_count: int = 0                # corpus size behind the subject
    signal: str = ""                  # human line for events/UI
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReelAssignment:
    """A commissioned reel: the chosen candidate plus the editor's angle."""
    candidate: ScoutCandidate
    angle: str = ""
    reason: str = ""


class _Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: int = Field(
        description="The number of the chosen candidate from the slate.")
    angle: str = Field(
        default="",
        description="One line: the sharpest hook this reel should lead with"
                    " — the most surprising number or highest stake in the"
                    " candidate's signals.")
    reason: str = Field(
        default="",
        description="One short sentence: why this subject makes a reel"
                    " people stop for.")


class _Assignments(BaseModel):
    """The assignment editor's commission for this production run."""
    model_config = ConfigDict(extra="forbid")
    picks: list[_Pick] = Field(
        default_factory=list,
        description="The chosen candidates, most reel-worthy first.")


_SYSTEM = (
    "You are the ASSIGNMENT EDITOR for a short-video news desk (Instagram"
    " Reels). You see a numbered slate of candidate subjects scouted from"
    " the last {hours} hours of coverage, each with signals: how many"
    " distinct outlets corroborate it (heat), how much coverage exists, and"
    " what kind of subject it is.\nCommission the {count} subjects that make"
    " the BEST reels. A great reel subject:\n"
    "- has a number, stake or reversal that lands in the first two seconds;\n"
    "- is CONCRETE and visual (a thing that happened — not an abstract"
    " category);\n"
    "- is corroborated (higher heat beats a single-outlet story);\n"
    "- is fresh — this is short video, not an explainer archive.\n"
    "Prefer thread/event candidates over bare topic tags (they are real"
    " stories, not categories). Never pick two candidates telling the same"
    " story. Pick ONLY from the slate, by number.")


# -- half 1: deterministic candidate SQL ---------------------------------------


def _workspace_focus(workspace: Any) -> tuple[str, list[Any]]:
    """A ' AND (...)' SQL fragment (referencing the ``d`` document alias) that
    scopes a scout query to a workspace's focus — its topics OR its sources OR
    its FTS query — reusing the same building blocks as ``focused_feed``. Empty
    string when there's no workspace or it has no focus set."""
    if workspace is None:
        return "", []
    from connect.storage.workspaces import topic_focus_clauses
    clauses, params = topic_focus_clauses(getattr(workspace, "topics", []) or [])
    source_ids = getattr(workspace, "source_ids", []) or []
    if source_ids:
        clauses.append("d.source_id = ANY(%s)")
        params.append(list(source_ids))
    query_fts = getattr(workspace, "query_fts", None)
    if query_fts:
        clauses.append("d.search_tsv @@ websearch_to_tsquery('english', %s)")
        params.append(query_fts)
    if not clauses:
        return "", []
    return " AND (" + " OR ".join(clauses) + ")", params


async def scout_candidates(conn: psycopg.AsyncConnection, *,
                           window_hours: int = 24,
                           dedup_days: int = 3,
                           limit: int = MAX_CANDIDATES,
                           workspace: Any = None
                           ) -> list[ScoutCandidate]:
    """The candidate slate: hot threads, then fresh events, then hot topics,
    deduped against subjects a recent campaign already covered. Zero LLM. When
    ``workspace`` is given, every query is scoped to that workspace's focus."""
    out: list[ScoutCandidate] = []
    seen_subjects: set[str] = set()
    focus = _workspace_focus(workspace)

    recent = await _recent_campaign_subjects(conn, days=dedup_days)

    def _admit(c: ScoutCandidate) -> None:
        key = c.subject.strip().lower()
        if not key or key in seen_subjects:
            return
        if any(key == r or key in r or r in key for r in recent):
            return
        seen_subjects.add(key)
        out.append(c)

    for c in await _hot_threads(conn, window_hours, focus):
        _admit(c)
    for c in await _fresh_events(conn, window_hours, focus):
        _admit(c)
    if len(out) < limit:
        for c in await _hot_topics(conn, window_hours, focus):
            _admit(c)
    return out[:limit]


async def _recent_campaign_subjects(conn: psycopg.AsyncConnection, *,
                                    days: int) -> set[str]:
    cur = await conn.execute(
        "SELECT subject FROM campaign WHERE created_at >= now() -"
        " make_interval(days => %s) AND status <> 'failed'", (days,))
    return {(r["subject"] or "").strip().lower()
            for r in await cur.fetchall()}


async def _hot_threads(conn: psycopg.AsyncConnection, window_hours: int,
                       focus: tuple[str, list[Any]] = ("", [])
                       ) -> list[ScoutCandidate]:
    """Active story threads with events created in the window, ranked by
    heat = distinct tier-1/2 outlets behind those new events (the briefing
    thread-move metric, hour-windowed)."""
    focus_sql, focus_params = focus
    cur = await conn.execute(
        "SELECT s.id, s.title, s.doc_count,"
        " COUNT(DISTINCT ev.id) AS new_events,"
        " COUNT(DISTINCT d.source_id) FILTER"
        "   (WHERE src.credibility_tier <= 2) AS heat,"
        " COUNT(DISTINCT d.source_id) AS n_sources"
        " FROM story s"
        " JOIN event ev ON ev.story_id = s.id"
        " JOIN event_assignment ea ON ea.event_id = ev.id"
        " JOIN document d ON d.id = ea.document_id"
        " LEFT JOIN source src ON src.id = d.source_id"
        " WHERE s.status = 'active'"
        " AND ev.created_at >= now() - make_interval(hours => %s)"
        f"{focus_sql}"
        " GROUP BY s.id, s.title, s.doc_count"
        " ORDER BY heat DESC, n_sources DESC, new_events DESC, s.id"
        " LIMIT 12", (window_hours, *focus_params))
    rows = await cur.fetchall()
    return [
        ScoutCandidate(
            subject=(r["title"] or "").strip(), kind="thread",
            story_id=int(r["id"]), heat=int(r["heat"] or 0),
            doc_count=int(r["doc_count"] or 0),
            signal=f"{r['new_events']} new event(s),"
                   f" {r['n_sources']} outlet(s)")
        for r in rows if (r["title"] or "").strip()]


async def _fresh_events(conn: psycopg.AsyncConnection, window_hours: int,
                        focus: tuple[str, list[Any]] = ("", [])
                        ) -> list[ScoutCandidate]:
    """Fresh event clusters NOT yet part of a thread (threaded ones surface
    above with the whole thread's context), ranked by corroboration."""
    focus_sql, focus_params = focus
    cur = await conn.execute(
        "SELECT ev.id, ev.title, ev.event_type, ev.doc_count,"
        " COUNT(DISTINCT d.source_id) FILTER"
        "   (WHERE src.credibility_tier <= 2) AS heat,"
        " COUNT(DISTINCT d.source_id) AS n_sources"
        " FROM event ev"
        " JOIN event_assignment ea ON ea.event_id = ev.id"
        " JOIN document d ON d.id = ea.document_id"
        " LEFT JOIN source src ON src.id = d.source_id"
        " WHERE ev.story_id IS NULL"
        # last_seen_at is a DATE (day precision): compare at day granularity,
        # else the midnight promotion silently excludes yesterday's events
        # from a 24h window (and everything from windows under a day).
        " AND ev.last_seen_at >="
        "     (now() - make_interval(hours => %s))::date"
        f"{focus_sql}"
        " GROUP BY ev.id, ev.title, ev.event_type, ev.doc_count"
        " ORDER BY heat DESC, n_sources DESC, ev.doc_count DESC, ev.id"
        " LIMIT 12", (window_hours, *focus_params))
    rows = await cur.fetchall()
    return [
        ScoutCandidate(
            subject=(r["title"] or "").strip(), kind="event",
            heat=int(r["heat"] or 0), doc_count=int(r["doc_count"] or 0),
            signal=f"{r['event_type'] or 'event'},"
                   f" {r['n_sources']} outlet(s)")
        for r in rows if (r["title"] or "").strip()]


async def _hot_topics(conn: psycopg.AsyncConnection, window_hours: int,
                      focus: tuple[str, list[Any]] = ("", [])
                      ) -> list[ScoutCandidate]:
    """Topic tags spiking in the window — the fallback when clustering is
    thin. A bare tag ('taxation') is a weaker subject than an event title,
    so these rank below threads/events on the slate."""
    focus_sql, focus_params = focus
    cur = await conn.execute(
        "SELECT dt.topic, COUNT(*) AS docs,"
        " COUNT(DISTINCT d.source_id) FILTER"
        "   (WHERE src.credibility_tier <= 2) AS heat,"
        " COUNT(DISTINCT d.source_id) AS n_sources"
        " FROM document_topic dt"
        " JOIN document d ON d.id = dt.document_id"
        " LEFT JOIN source src ON src.id = d.source_id"
        " WHERE d.fetched_at >= now() - make_interval(hours => %s)"
        " AND d.visibility = 'shared' AND d.canonical_document_id IS NULL"
        " AND dt.topic <> 'other'"
        f"{focus_sql}"
        " GROUP BY dt.topic"
        " HAVING COUNT(DISTINCT d.source_id) >= 2"
        " ORDER BY heat DESC, n_sources DESC, docs DESC LIMIT 8",
        (window_hours, *focus_params))
    rows = await cur.fetchall()
    return [
        ScoutCandidate(
            subject=str(r["topic"]).replace("-", " "), kind="topic",
            heat=int(r["heat"] or 0), doc_count=int(r["docs"] or 0),
            signal=f"{r['docs']} docs, {r['n_sources']} outlet(s)")
        for r in rows]


# -- half 2: the assignment editor ---------------------------------------------


def _slate_text(candidates: list[ScoutCandidate]) -> str:
    lines = []
    for i, c in enumerate(candidates, start=1):
        lines.append(
            f"{i}. [{c.kind}] {c.subject} — heat {c.heat},"
            f" {c.doc_count} doc(s); {c.signal}")
    return "\n".join(lines)


def _line(text: str | None, cap: int) -> str:
    """Bounded, marker-free one-liner (rides into prompts, jsonb and UI)."""
    return " ".join(_MARKER_RE.sub(" ", text or "").split())[:cap].strip()


def _fallback(candidates: list[ScoutCandidate],
              count: int) -> list[ReelAssignment]:
    """No LLM (or a bad commission): take the slate top — it is already
    ordered threads > events > topics, hottest first."""
    return [ReelAssignment(candidate=c, reason="top of the scout slate")
            for c in candidates[:count]]


async def pick_assignments(conn: psycopg.AsyncConnection,
                           provider: LLMProvider | None, *,
                           candidates: list[ScoutCandidate], count: int,
                           window_hours: int, budget: AnalysisBudget,
                           governor: Any, viewer: int
                           ) -> list[ReelAssignment]:
    """The K commissioned reels. One FAST call; any failure degrades to the
    deterministic slate-top pick so the factory always produces."""
    if not candidates:
        return []
    count = max(1, min(count, len(candidates)))
    if provider is None:
        return _fallback(candidates, count)

    system = _SYSTEM.format(hours=window_hours, count=count)
    user = (f"CANDIDATE SLATE:\n{_slate_text(candidates)}\n\n"
            f"Commission the {count} best reel subjects now.")
    proj = spend.cost_usd(provider.model_for(ModelTier.FAST),
                          input_tokens=EST_IN, output_tokens=EST_OUT)
    try:
        # budget/governor denials degrade like any other picker failure —
        # the deterministic slate costs nothing, and a scheduled run that
        # hits the daily envelope must still produce (there is no retry
        # until the next UTC day).
        budget.check(proj)
        await governor.check(proj, user_id=viewer)
        completion = await provider.complete_structured(
            system=system, messages=[{"role": "user", "content": user}],
            schema=_Assignments, tier=ModelTier.FAST,
            max_tokens=PICK_MAX_TOKENS)
    except (LLMError, ValidationError, BudgetExceeded,
            AnalysisBudgetExceeded) as e:
        log.warning("reel scout picker failed, using slate order: %s", e)
        return _fallback(candidates, count)
    await spend.record_call(conn, purpose=PURPOSE, model=completion.model,
                            usage=completion.usage, user_id=viewer)
    budget.add(spend.cost_usd(
        completion.model, input_tokens=completion.usage.input_tokens,
        output_tokens=completion.usage.output_tokens,
        cache_read_tokens=completion.usage.cache_read_tokens))

    picks: list[ReelAssignment] = []
    used: set[int] = set()
    for p in completion.output.picks:
        i = p.candidate - 1
        if i < 0 or i >= len(candidates) or i in used:
            continue
        used.add(i)
        picks.append(ReelAssignment(
            candidate=candidates[i],
            angle=_line(p.angle, MAX_ANGLE),
            reason=_line(p.reason, MAX_ANGLE)))
        if len(picks) == count:
            break
    return picks or _fallback(candidates, count)
