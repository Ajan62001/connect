"""Position tracking — shift detection + cached evolution summaries (v9).

Shift detection runs after a document's statements persist: for each
(speaker, topic) pair with a NEW statement and >= 1 prior statement, ONE
FAST structured call compares the new quote against up to
MAX_PRIOR_STATEMENTS prior quotes presented as a closed id menu. The
verdict's versus_statement_id must come from that menu (an off-menu id is
rejected in code — closed-menu discipline) and only 'shifted'/'reversed'
produce position_shift rows. Both sides of a shift are span-verified
verbatim quotes, so shifts are grounded by construction. Spend is ledgered
under the general daily budget (purpose 'enrichment'); BudgetExceeded stops
the loop cleanly.

Evolution summaries are cached per (entity, topic) in view_summary and
regenerated lazily ON READ when stale — the statement count grew by
>= STALE_STATEMENT_GROWTH since generation OR a shift was detected after
generated_at. Regeneration is ONE FAST call over the closed statement menu
(id -> quote + date + source) producing text with [[s<id>]] citation
markers; post-hoc every marker must resolve to a menu id (invalid markers
are stripped and counted). A fresh summary is served from the cache — reads
are $0; on governor block / missing provider the stale text is served with
stale=True, never an error.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from connect.domain.models import EvolutionSummary
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.storage.db import utc_now

log = logging.getLogger(__name__)

PURPOSE_POSITION = "enrichment"     # general daily envelope, never investigation
SHIFT_PROMPT_VERSION = "position-shift-v1"
VIEW_SUMMARY_PROMPT_VERSION = "view-summary-v1"

MAX_PRIOR_STATEMENTS = 5
MAX_NOTE_CHARS = 160
STALE_STATEMENT_GROWTH = 2          # regenerate after >= 2 new statements
MAX_SUMMARY_STATEMENTS = 20         # menu cap for the evolution summary

EST_SHIFT_INPUT_TOKENS = 900
EST_SHIFT_OUTPUT_TOKENS = 120
EST_SUMMARY_INPUT_TOKENS = 1500
EST_SUMMARY_OUTPUT_TOKENS = 400

_MARKER_RE = re.compile(r"\[\[s(\d+)\]\]")
# Observed live: the model fuses several citations into ONE bracket
# ("[[s1,s2]]", "[[s1, s2]]") — normalized into adjacent single markers
# before validation so the ids are not silently lost.
_COMPOUND_MARKER_RE = re.compile(r"\[\[s\d+(?:\s*,\s*s?\d+)+\]\]")
# Any other [[...]] token left after validation is malformed — stripped, so
# bracket junk never reaches the frontend (which only renders [[s<id>]]).
_LEFTOVER_MARKER_RE = re.compile(r"\[\[(?!s\d+\]\])[^\[\]]*\]\]")

SHIFT_SYSTEM = """You compare a public figure's NEW statement on a topic \
against their PRIOR statements on the same topic and judge how the position \
moved. All statements are verbatim quotes from news documents.

relation must be exactly one of:
- consistent: the new statement takes the same position as before.
- elaborated: same position, but with new detail, scope or conditions.
- shifted: the position moved materially (softened, hardened, new caveats \
that change the stance) without being the opposite.
- reversed: the new statement takes the OPPOSITE position.

versus_statement_id: the id of the ONE prior statement (from the supplied \
menu) the new statement most directly relates to. Use ONLY ids from the \
menu.

note: at most 160 characters, neutral register, naming what changed (empty \
for consistent). Be conservative: rhetorical restatements are consistent, \
not shifts."""

SUMMARY_SYSTEM = """You write a short neutral summary (3-5 sentences) of \
how a public figure's position on one topic evolved over time, based ONLY \
on the menu of verbatim statements supplied (chronological order). After \
every assertion, cite the statement(s) it rests on with markers of the \
exact form [[s<id>]] using ONLY ids from the menu — e.g. "Initially \
opposed the bill [[s12]]". Never invent statements, dates or ids; if the \
statements are consistent, say so plainly."""


class ShiftJudgment(BaseModel):
    model_config = ConfigDict(frozen=True)

    relation: Literal["consistent", "elaborated", "shifted", "reversed"]
    versus_statement_id: int
    note: str = ""

    @field_validator("note")
    @classmethod
    def _clamp_note(cls, v: str) -> str:
        return v.strip()[:MAX_NOTE_CHARS]


class EvolutionSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str


# -- shift detection ------------------------------------------------------------------


def _topics_of(row: sqlite3.Row) -> list[str]:
    try:
        topics = json.loads(row["topics"] or "[]")
    except (ValueError, TypeError):
        return []
    return [t for t in topics if isinstance(t, str)]


def _prior_statements(conn: sqlite3.Connection, *, entity_id: int,
                      topic: str, before: sqlite3.Row,
                      limit: int = MAX_PRIOR_STATEMENTS) -> list[sqlite3.Row]:
    """Up to ``limit`` prior statements by the speaker on the topic, from
    OTHER documents, chronological (oldest first)."""
    rows = conn.execute(
        "SELECT s.id, s.quote, s.position_summary, s.stated_at"
        " FROM statement s"
        " WHERE s.entity_id = ? AND s.document_id != ?"
        " AND EXISTS (SELECT 1 FROM json_each(s.topics) je"
        "             WHERE je.value = ?)"
        " AND (s.stated_at < ? OR (s.stated_at = ? AND s.id < ?))"
        " ORDER BY s.stated_at DESC, s.id DESC LIMIT ?",
        (entity_id, before["document_id"], topic, before["stated_at"],
         before["stated_at"], before["id"], limit)).fetchall()
    return list(reversed(rows))


def _shift_message(speaker: str, topic: str, new: sqlite3.Row,
                   priors: list[sqlite3.Row]) -> str:
    lines = [f"SPEAKER: {speaker}", f"TOPIC: {topic}", "",
             "PRIOR STATEMENTS (chronological; cite by id):"]
    for p in priors:
        stance = f" — stance: {p['position_summary']}" \
            if p["position_summary"] else ""
        lines.append(f"[{p['id']}] ({p['stated_at'] or 'undated'})"
                     f" \"{p['quote']}\"{stance}")
    stance = f" — stance: {new['position_summary']}" \
        if new["position_summary"] else ""
    lines += ["", "NEW STATEMENT:",
              f"({new['stated_at'] or 'undated'}) \"{new['quote']}\"{stance}",
              "", "How did the speaker's position move?"]
    return "\n".join(lines)


def _open_shift_exists(conn: sqlite3.Connection, a: int, b: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM position_shift WHERE status = 'open'"
        " AND ((from_statement_id = ? AND to_statement_id = ?)"
        "   OR (from_statement_id = ? AND to_statement_id = ?))",
        (a, b, b, a)).fetchone() is not None


async def detect_shifts(conn: sqlite3.Connection,
                        provider: LLMProvider | None, governor: Governor,
                        document_id: int) -> dict[str, Any]:
    """Run shift detection for every (speaker, topic) pair that gained a
    statement from ``document_id``. Degrades to nothing without a provider
    or budget — never raises into the T1 path."""
    stats: dict[str, Any] = {"pairs": 0, "calls": 0, "shifts": 0,
                             "rejected_versus": 0, "skipped_duplicate": 0,
                             "halted_budget": False}
    if provider is None:
        return stats
    new_rows = conn.execute(
        "SELECT s.id, s.document_id, s.entity_id, s.quote,"
        " s.position_summary, s.stated_at, s.topics, e.name AS speaker"
        " FROM statement s JOIN entity e ON e.id = s.entity_id"
        " WHERE s.document_id = ? ORDER BY s.id", (document_id,)).fetchall()
    # one probe per (speaker, topic): the doc's newest statement for the pair
    probes: dict[tuple[int, str], sqlite3.Row] = {}
    for row in new_rows:
        for topic in _topics_of(row):
            probes[(row["entity_id"], topic)] = row

    model = provider.model_for(ModelTier.FAST)
    for (entity_id, topic), new in probes.items():
        priors = _prior_statements(conn, entity_id=entity_id, topic=topic,
                                   before=new)
        if not priors:
            continue
        stats["pairs"] += 1
        try:
            governor.check(spend.cost_usd(
                model, input_tokens=EST_SHIFT_INPUT_TOKENS,
                output_tokens=EST_SHIFT_OUTPUT_TOKENS))
        except BudgetExceeded as e:
            log.warning("shift detection halted by governor (doc %s): %s",
                        document_id, e)
            stats["halted_budget"] = True
            break
        try:
            completion = await provider.complete_structured(
                system=SHIFT_SYSTEM,
                messages=[{"role": "user",
                           "content": _shift_message(
                               new["speaker"], topic, new, priors)}],
                schema=ShiftJudgment, tier=ModelTier.FAST, max_tokens=256)
        except LLMError as e:
            log.warning("shift judgment failed (entity %s, topic %s): %s",
                        entity_id, topic, e)
            continue
        spend.record_call(conn, purpose=PURPOSE_POSITION,
                          model=completion.model, usage=completion.usage)
        stats["calls"] += 1
        verdict = completion.output
        if verdict.relation not in ("shifted", "reversed"):
            continue
        if verdict.versus_statement_id not in {p["id"] for p in priors}:
            log.warning("shift verdict cited off-menu statement %s"
                        " (entity %s, topic %s) — rejected",
                        verdict.versus_statement_id, entity_id, topic)
            stats["rejected_versus"] += 1
            continue
        if _open_shift_exists(conn, verdict.versus_statement_id, new["id"]):
            stats["skipped_duplicate"] += 1
            continue
        with conn:
            conn.execute(
                "INSERT INTO position_shift (entity_id, topic,"
                " from_statement_id, to_statement_id, kind, note,"
                " detected_at, status) VALUES (?,?,?,?,?,?,?, 'open')",
                (entity_id, topic, verdict.versus_statement_id, new["id"],
                 verdict.relation, verdict.note or None, utc_now()))
        stats["shifts"] += 1
    return stats


# -- evolution summaries ----------------------------------------------------------------


def statement_count(conn: sqlite3.Connection, entity_id: int,
                    topic: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM statement s WHERE s.entity_id = ?"
        " AND EXISTS (SELECT 1 FROM json_each(s.topics) je"
        "             WHERE je.value = ?)", (entity_id, topic)).fetchone()[0])


def _summary_row(conn: sqlite3.Connection, entity_id: int,
                 topic: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT text, citations, statement_count_at_gen, generated_at"
        " FROM view_summary WHERE entity_id = ? AND topic = ?",
        (entity_id, topic)).fetchone()


def is_stale(conn: sqlite3.Connection, entity_id: int, topic: str, *,
             count: int | None = None) -> bool:
    """Staleness: no cached summary, the statement count grew by
    >= STALE_STATEMENT_GROWTH since generation, or a shift was detected
    after generated_at."""
    row = _summary_row(conn, entity_id, topic)
    if row is None or not row["generated_at"]:
        return True
    if count is None:
        count = statement_count(conn, entity_id, topic)
    if count >= (row["statement_count_at_gen"] or 0) + STALE_STATEMENT_GROWTH:
        return True
    return conn.execute(
        "SELECT 1 FROM position_shift WHERE entity_id = ? AND topic = ?"
        " AND detected_at > ? LIMIT 1",
        (entity_id, topic, row["generated_at"])).fetchone() is not None


def _to_summary(row: sqlite3.Row, *, stale: bool) -> EvolutionSummary | None:
    if row is None or not row["generated_at"]:
        return None
    try:
        citations = [int(c) for c in json.loads(row["citations"] or "[]")]
    except (ValueError, TypeError):
        citations = []
    return EvolutionSummary(text=row["text"] or "", citations=citations,
                            generated_at=row["generated_at"], stale=stale)


def _summary_menu(conn: sqlite3.Connection, entity_id: int,
                  topic: str) -> list[sqlite3.Row]:
    """The closed statement menu: most recent MAX_SUMMARY_STATEMENTS for the
    (entity, topic) pair, chronological (oldest first)."""
    rows = conn.execute(
        "SELECT s.id, s.quote, s.position_summary, s.stated_at,"
        " src.name AS source_name"
        " FROM statement s"
        " JOIN document d ON d.id = s.document_id"
        " LEFT JOIN source src ON src.id = d.source_id"
        " WHERE s.entity_id = ?"
        " AND EXISTS (SELECT 1 FROM json_each(s.topics) je"
        "             WHERE je.value = ?)"
        " ORDER BY s.stated_at DESC, s.id DESC LIMIT ?",
        (entity_id, topic, MAX_SUMMARY_STATEMENTS)).fetchall()
    return list(reversed(rows))


def _summary_message(speaker: str, topic: str,
                     menu: list[sqlite3.Row]) -> str:
    lines = [f"SPEAKER: {speaker}", f"TOPIC: {topic}", "",
             "STATEMENTS (chronological; cite as [[s<id>]]):"]
    for m in menu:
        src = m["source_name"] or "unknown source"
        lines.append(f"[s{m['id']}] ({m['stated_at'] or 'undated'}, {src})"
                     f" \"{m['quote']}\"")
    lines += ["", "Write the evolution summary."]
    return "\n".join(lines)


def validate_markers(text: str, allowed_ids: set[int],
                     ) -> tuple[str, list[int], int]:
    """Post-hoc closed-menu check: every [[s<id>]] marker must resolve to a
    menu id. Compound markers ("[[s1,s2]]") are first expanded into single
    markers; any other malformed [[...]] token is stripped and counted.
    Returns (text with invalid markers stripped, cited ids in order of
    first appearance, invalid-marker count)."""
    citations: list[int] = []
    invalid = 0

    def _expand(match: re.Match[str]) -> str:
        return "".join(f"[[s{i}]]" for i in re.findall(r"\d+",
                                                       match.group(0)))

    def _sub(match: re.Match[str]) -> str:
        nonlocal invalid
        sid = int(match.group(1))
        if sid in allowed_ids:
            if sid not in citations:
                citations.append(sid)
            return match.group(0)
        invalid += 1
        return ""

    def _strip_leftover(match: re.Match[str]) -> str:
        nonlocal invalid
        invalid += 1
        return ""

    cleaned = _MARKER_RE.sub(_sub, _COMPOUND_MARKER_RE.sub(_expand, text))
    cleaned = _LEFTOVER_MARKER_RE.sub(_strip_leftover, cleaned)
    return cleaned, citations, invalid


async def get_view_summary(conn: sqlite3.Connection,
                           provider: LLMProvider | None, governor: Governor,
                           entity_id: int, topic: str,
                           ) -> EvolutionSummary | None:
    """The (entity, topic) evolution summary — cached; regenerated lazily
    when stale. Returns None when there are no statements and no cache; a
    governor block / missing provider serves the stale cache (stale=True)."""
    count = statement_count(conn, entity_id, topic)
    cached = _summary_row(conn, entity_id, topic)
    if not is_stale(conn, entity_id, topic, count=count):
        return _to_summary(cached, stale=False)
    if count == 0 or provider is None:
        return _to_summary(cached, stale=True)

    model = provider.model_for(ModelTier.FAST)
    try:
        governor.check(spend.cost_usd(
            model, input_tokens=EST_SUMMARY_INPUT_TOKENS,
            output_tokens=EST_SUMMARY_OUTPUT_TOKENS))
    except BudgetExceeded as e:
        log.warning("view summary regeneration blocked by governor"
                    " (entity %s, topic %s): %s", entity_id, topic, e)
        return _to_summary(cached, stale=True)

    speaker = conn.execute("SELECT name FROM entity WHERE id = ?",
                           (entity_id,)).fetchone()
    if speaker is None:
        return None
    menu = _summary_menu(conn, entity_id, topic)
    try:
        completion = await provider.complete_structured(
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user",
                       "content": _summary_message(speaker["name"], topic,
                                                   menu)}],
            schema=EvolutionSummaryOut, tier=ModelTier.FAST, max_tokens=512)
    except LLMError as e:
        log.warning("view summary generation failed (entity %s, topic %s):"
                    " %s", entity_id, topic, e)
        return _to_summary(cached, stale=True)
    spend.record_call(conn, purpose=PURPOSE_POSITION,
                      model=completion.model, usage=completion.usage)

    text, citations, invalid = validate_markers(
        completion.output.text, {m["id"] for m in menu})
    if invalid:
        log.warning("view summary cited %s off-menu statement id(s)"
                    " (entity %s, topic %s) — markers stripped",
                    invalid, entity_id, topic)
    generated_at = utc_now()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO view_summary (entity_id, topic, text,"
            " citations, statement_count_at_gen, generated_at)"
            " VALUES (?,?,?,?,?,?)",
            (entity_id, topic, text, json.dumps(citations), count,
             generated_at))
    return EvolutionSummary(text=text, citations=citations,
                            generated_at=generated_at, stale=False)
