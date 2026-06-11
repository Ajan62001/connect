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


import logging
import re
from typing import Any, Literal, Mapping

import psycopg
from pydantic import BaseModel, ConfigDict, field_validator

from connect.domain.models import EvolutionSummary
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.storage.pg import Jsonb, utc_now

_Row = Mapping[str, Any]

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


def _topics_of(row: _Row) -> list[str]:
    topics = row["topics"]
    if not isinstance(topics, list):
        return []
    return [t for t in topics if isinstance(t, str)]


async def _prior_statements(conn: psycopg.AsyncConnection, *,
                            entity_id: int, topic: str, before: _Row,
                            limit: int = MAX_PRIOR_STATEMENTS) -> list[_Row]:
    """Up to ``limit`` prior statements by the speaker on the topic, from
    OTHER documents, chronological (oldest first)."""
    cur = await conn.execute(
        "SELECT s.id, s.quote, s.position_summary, s.stated_at"
        " FROM statement s"
        " WHERE s.entity_id = %s AND s.document_id != %s"
        " AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(s.topics) je"
        "             WHERE je.value = %s)"
        " AND (s.stated_at < %s OR (s.stated_at = %s AND s.id < %s))"
        " ORDER BY s.stated_at DESC, s.id DESC LIMIT %s",
        (entity_id, before["document_id"], topic, before["stated_at"],
         before["stated_at"], before["id"], limit))
    rows = await cur.fetchall()
    return list(reversed(rows))


def _shift_message(speaker: str, topic: str, new: _Row,
                   priors: list[_Row]) -> str:
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


async def _open_shift_exists(conn: psycopg.AsyncConnection, a: int,
                             b: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM position_shift WHERE status = 'open'"
        " AND ((from_statement_id = %s AND to_statement_id = %s)"
        "   OR (from_statement_id = %s AND to_statement_id = %s))",
        (a, b, b, a))
    return await cur.fetchone() is not None


async def detect_shifts(conn: psycopg.AsyncConnection,
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
    cur = await conn.execute(
        "SELECT s.id, s.document_id, s.entity_id, s.quote,"
        " s.position_summary, s.stated_at, s.topics, e.name AS speaker"
        " FROM statement s JOIN entity e ON e.id = s.entity_id"
        " WHERE s.document_id = %s ORDER BY s.id", (document_id,))
    new_rows = await cur.fetchall()
    # one probe per (speaker, topic): the doc's newest statement for the pair
    probes: dict[tuple[int, str], _Row] = {}
    for row in new_rows:
        for topic in _topics_of(row):
            probes[(row["entity_id"], topic)] = row

    model = provider.model_for(ModelTier.FAST)
    for (entity_id, topic), new in probes.items():
        priors = await _prior_statements(conn, entity_id=entity_id,
                                         topic=topic, before=new)
        if not priors:
            continue
        stats["pairs"] += 1
        try:
            await governor.check(spend.cost_usd(
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
        await spend.record_call(conn, purpose=PURPOSE_POSITION,
                                model=completion.model,
                                usage=completion.usage)
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
        if await _open_shift_exists(conn, verdict.versus_statement_id,
                                    new["id"]):
            stats["skipped_duplicate"] += 1
            continue
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO position_shift (entity_id, topic,"
                " from_statement_id, to_statement_id, kind, note,"
                " detected_at, status) VALUES (%s,%s,%s,%s,%s,%s,%s, 'open')",
                (entity_id, topic, verdict.versus_statement_id, new["id"],
                 verdict.relation, verdict.note or None, utc_now()))
        stats["shifts"] += 1
    return stats


# -- evolution summaries ----------------------------------------------------------------


async def statement_count(conn: psycopg.AsyncConnection, entity_id: int,
                          topic: str) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM statement s WHERE s.entity_id = %s"
        " AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(s.topics) je"
        "             WHERE je.value = %s)", (entity_id, topic))
    return int((await cur.fetchone())["n"])


async def _summary_row(conn: psycopg.AsyncConnection, entity_id: int,
                       topic: str) -> _Row | None:
    cur = await conn.execute(
        "SELECT text, citations, statement_count_at_gen, generated_at"
        " FROM view_summary WHERE entity_id = %s AND topic = %s",
        (entity_id, topic))
    return await cur.fetchone()


async def is_stale(conn: psycopg.AsyncConnection, entity_id: int,
                   topic: str, *, count: int | None = None) -> bool:
    """Staleness: no cached summary, the statement count grew by
    >= STALE_STATEMENT_GROWTH since generation, or a shift was detected
    after generated_at."""
    row = await _summary_row(conn, entity_id, topic)
    if row is None or not row["generated_at"]:
        return True
    if count is None:
        count = await statement_count(conn, entity_id, topic)
    if count >= (row["statement_count_at_gen"] or 0) + STALE_STATEMENT_GROWTH:
        return True
    cur = await conn.execute(
        "SELECT 1 FROM position_shift WHERE entity_id = %s AND topic = %s"
        " AND detected_at > %s LIMIT 1",
        (entity_id, topic, row["generated_at"]))
    return await cur.fetchone() is not None


def _to_summary(row: _Row, *, stale: bool) -> EvolutionSummary | None:
    if row is None or not row["generated_at"]:
        return None
    raw = row["citations"]
    citations = ([int(c) for c in raw] if isinstance(raw, list) else [])
    return EvolutionSummary(text=row["text"] or "", citations=citations,
                            generated_at=row["generated_at"], stale=stale)


async def _summary_menu(conn: psycopg.AsyncConnection, entity_id: int,
                        topic: str) -> list[_Row]:
    """The closed statement menu: most recent MAX_SUMMARY_STATEMENTS for the
    (entity, topic) pair, chronological (oldest first)."""
    cur = await conn.execute(
        "SELECT s.id, s.quote, s.position_summary, s.stated_at,"
        " src.name AS source_name"
        " FROM statement s"
        " JOIN document d ON d.id = s.document_id"
        " LEFT JOIN source src ON src.id = d.source_id"
        " WHERE s.entity_id = %s"
        " AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(s.topics) je"
        "             WHERE je.value = %s)"
        " ORDER BY s.stated_at DESC, s.id DESC LIMIT %s",
        (entity_id, topic, MAX_SUMMARY_STATEMENTS))
    rows = await cur.fetchall()
    return list(reversed(rows))


def _summary_message(speaker: str, topic: str,
                     menu: list[_Row]) -> str:
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


async def get_view_summary(conn: psycopg.AsyncConnection,
                           provider: LLMProvider | None, governor: Governor,
                           entity_id: int, topic: str,
                           ) -> EvolutionSummary | None:
    """The (entity, topic) evolution summary — cached; regenerated lazily
    when stale. Returns None when there are no statements and no cache; a
    governor block / missing provider serves the stale cache (stale=True)."""
    count = await statement_count(conn, entity_id, topic)
    cached = await _summary_row(conn, entity_id, topic)
    if not await is_stale(conn, entity_id, topic, count=count):
        return _to_summary(cached, stale=False)
    if count == 0 or provider is None:
        return _to_summary(cached, stale=True)

    model = provider.model_for(ModelTier.FAST)
    try:
        await governor.check(spend.cost_usd(
            model, input_tokens=EST_SUMMARY_INPUT_TOKENS,
            output_tokens=EST_SUMMARY_OUTPUT_TOKENS))
    except BudgetExceeded as e:
        log.warning("view summary regeneration blocked by governor"
                    " (entity %s, topic %s): %s", entity_id, topic, e)
        return _to_summary(cached, stale=True)

    cur = await conn.execute("SELECT name FROM entity WHERE id = %s",
                             (entity_id,))
    speaker = await cur.fetchone()
    if speaker is None:
        return None
    menu = await _summary_menu(conn, entity_id, topic)
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
    await spend.record_call(conn, purpose=PURPOSE_POSITION,
                            model=completion.model, usage=completion.usage)

    text, citations, invalid = validate_markers(
        completion.output.text, {m["id"] for m in menu})
    if invalid:
        log.warning("view summary cited %s off-menu statement id(s)"
                    " (entity %s, topic %s) — markers stripped",
                    invalid, entity_id, topic)
    generated_at = utc_now()
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO view_summary (entity_id, topic, text,"
            " citations, statement_count_at_gen, generated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (entity_id, topic) DO UPDATE SET"
            " text = EXCLUDED.text, citations = EXCLUDED.citations,"
            " statement_count_at_gen = EXCLUDED.statement_count_at_gen,"
            " generated_at = EXCLUDED.generated_at",
            (entity_id, topic, text, Jsonb(citations), count,
             generated_at))
    return EvolutionSummary(text=text, citations=citations,
                            generated_at=generated_at, stale=False)
