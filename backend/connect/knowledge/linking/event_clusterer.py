"""Event clustering — deterministic first, FAST-tier LLM only in the gray
zone (design doc result.pipeline §2).

1. Candidate retrieval: cosine of the doc embedding against event centroids
   for events whose event_type-specific activity window (event_type.window_days)
   covers the document date.
2. Score = 0.5*cosine + 0.3*entity_jaccard + 0.2*same_event_type.
3. score >= 0.80 -> attach; score <= 0.55 -> new event; gray zone -> ONE
   FAST-tier closed-menu adjudication (top-3 candidates vs NEW). Without a
   provider/budget the gray zone degrades to a new event — never blocks.
4. On attach: centroid running mean, doc_count/last_seen_at bump; event
   title+summary refreshed by FAST at cluster sizes 3/10/25 ONLY (log
   schedule — a 40-doc flood costs 3 summary calls, not 40).

EVERY decision writes an event_assignment audit row (method, score, and the
full adjudication payload for gray-zone calls) so thresholds can be tuned
against logged decisions.

T2 input is T1-enriched docs: entities exist (entity_mention) and the doc
embedding exists from T0 (document_embedding / vec_document).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, field_validator

from connect.knowledge.vector import _pack, _unpack
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.storage.db import utc_now

log = logging.getLogger(__name__)

# -- thresholds & weights (design doc result.pipeline) ---------------------------

ATTACH_THRESHOLD = 0.80
NEW_EVENT_THRESHOLD = 0.55
W_COSINE = 0.5
W_ENTITY_JACCARD = 0.3
W_SAME_EVENT_TYPE = 0.2
TOP_K_ADJUDICATION = 3
SUMMARY_REFRESH_SIZES = (3, 10, 25)
DEFAULT_WINDOW_DAYS = 7
SUMMARY_MAX_DOCS = 25

# micro-adjudication cost-projection assumptions (design doc: ~600/50)
EST_ADJ_INPUT_TOKENS = 600
EST_ADJ_OUTPUT_TOKENS = 50
EST_SUMMARY_INPUT_TOKENS = 1500
EST_SUMMARY_OUTPUT_TOKENS = 150

PURPOSE_ADJUDICATION = "event_adjudication"
PURPOSE_SUMMARY = "event_summary"
ADJ_PROMPT_VERSION = "event-adj-v1"
SUMMARY_PROMPT_VERSION = "event-sum-v1"

ADJ_SYSTEM = """You assign a news document to an event cluster. You receive \
the document (title, one-line summary, event type, date) and a CLOSED menu \
of numbered candidate events. Decide whether the document reports on one of \
the listed events or on a distinct new event.

Return choice: the candidate number (1-3) when the document covers the SAME \
real-world event (same actors, same action, same occasion — follow-up \
coverage of one occurrence counts; a separate later development does not), \
or 0 for NEW EVENT. Choose ONLY from the menu. When unsure, prefer 0."""

SUMMARY_SYSTEM = """You write the canonical title and summary for a news \
event cluster. You receive the member documents' titles and one-line \
summaries. Return:
- title: one specific headline-style line (<= 120 characters) naming the \
key actor and action; no dates unless essential.
- summary: 1-2 neutral sentences describing what happened, grounded ONLY \
in the provided lines. Never invent details."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class EventAdjudication(_Frozen):
    """Closed-menu verdict: 0 = NEW EVENT, 1..K = candidate number."""
    choice: int

    @field_validator("choice")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, v)


class EventSummary(_Frozen):
    title: str
    summary: str

    @field_validator("title")
    @classmethod
    def _clamp_title(cls, v: str) -> str:
        return v.strip()[:200]

    @field_validator("summary")
    @classmethod
    def _clamp_summary(cls, v: str) -> str:
        return v.strip()[:600]


@dataclass(frozen=True)
class Candidate:
    event_id: int
    title: str
    description: str | None
    event_type: str
    occurred_on: str | None
    doc_count: int
    cosine: float
    entity_jaccard: float
    same_event_type: bool
    score: float


@dataclass(frozen=True)
class AssignmentResult:
    assignment_id: int
    event_id: int
    method: str            # 'attach' | 'new' | 'adjudicated'
    score: float | None
    created_event: bool
    summary_refreshed: bool = False


# -- pure math -------------------------------------------------------------------

def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine clamped to [0, 1]; 0.0 when either vector is missing or the
    dimensions disagree (degraded-but-deterministic clustering)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = (math.sqrt(sum(x * x for x in a))
            * math.sqrt(sum(y * y for y in b))) or 1.0
    return max(0.0, min(1.0, dot / norm))


def entity_jaccard(a: set[int], b: set[int]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def score_candidate(*, cosine: float, jaccard: float,
                    same_event_type: bool) -> float:
    return (W_COSINE * cosine + W_ENTITY_JACCARD * jaccard
            + W_SAME_EVENT_TYPE * (1.0 if same_event_type else 0.0))


def update_centroid(centroid: list[float], n: int,
                    vector: list[float]) -> list[float]:
    """Running mean: the centroid of n members absorbs one more vector."""
    return [(c * n + v) / (n + 1) for c, v in zip(centroid, vector)]


# -- vector / entity plumbing ------------------------------------------------------

def get_document_vector(conn: sqlite3.Connection,
                        document_id: int) -> list[float] | None:
    row = conn.execute(
        "SELECT vector FROM document_embedding WHERE document_id = ?",
        (document_id,)).fetchone()
    if row is not None:
        return _unpack(row[0])
    try:  # sqlite-vec backend stores in vec_document instead
        row = conn.execute(
            "SELECT embedding FROM vec_document WHERE rowid = ?",
            (document_id,)).fetchone()
    except sqlite3.Error:
        return None
    return _unpack(row[0]) if row is not None else None


def get_event_centroid(conn: sqlite3.Connection,
                       event_id: int) -> list[float] | None:
    row = conn.execute(
        "SELECT vector FROM event_embedding WHERE event_id = ?",
        (event_id,)).fetchone()
    return _unpack(row[0]) if row is not None else None


def _set_event_centroid(conn: sqlite3.Connection, event_id: int,
                        vector: list[float], model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO event_embedding (event_id, model, dim,"
        " vector) VALUES (?,?,?,?)",
        (event_id, model, len(vector), _pack(vector)))


def document_entity_ids(conn: sqlite3.Connection,
                        document_id: int) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM entity_mention WHERE document_id = ?",
        (document_id,))}


def event_entity_ids(conn: sqlite3.Connection, event_id: int) -> set[int]:
    """Union of member documents' linked entities."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT m.entity_id FROM entity_mention m"
        " WHERE m.document_id IN (SELECT document_id FROM event_assignment"
        "                         WHERE event_id = ?)", (event_id,))}


# -- candidate retrieval -----------------------------------------------------------

def find_candidates(conn: sqlite3.Connection, *, doc_date: str,
                    doc_vector: list[float] | None,
                    doc_entities: set[int],
                    doc_event_type: str) -> list[Candidate]:
    """Score every event whose activity window covers the document date;
    best-first. Window length comes from the EVENT's event_type taxonomy row
    (event_type.window_days), measured from the event's last activity."""
    rows = conn.execute(
        "SELECT e.id, e.title, e.description, e.event_type, e.occurred_on,"
        " e.doc_count"
        " FROM event e LEFT JOIN event_type et ON et.name = e.event_type"
        " WHERE ABS(julianday(?) - julianday(COALESCE(e.last_seen_at,"
        "   e.occurred_on, substr(e.created_at, 1, 10))))"
        "   <= COALESCE(et.window_days, ?)",
        (doc_date, DEFAULT_WINDOW_DAYS)).fetchall()
    candidates: list[Candidate] = []
    for row in rows:
        cos = cosine_similarity(doc_vector, get_event_centroid(conn, row["id"]))
        jac = entity_jaccard(doc_entities, event_entity_ids(conn, row["id"]))
        same = row["event_type"] == doc_event_type
        candidates.append(Candidate(
            event_id=row["id"], title=row["title"],
            description=row["description"], event_type=row["event_type"],
            occurred_on=row["occurred_on"], doc_count=row["doc_count"] or 0,
            cosine=cos, entity_jaccard=jac, same_event_type=same,
            score=score_candidate(cosine=cos, jaccard=jac,
                                  same_event_type=same)))
    candidates.sort(key=lambda c: (-c.score, c.event_id))
    return candidates


# -- the decision -------------------------------------------------------------------

async def assign_document(conn: sqlite3.Connection,
                          provider: LLMProvider | None,
                          governor: Governor,
                          document_id: int) -> AssignmentResult | None:
    """Cluster one T1-enriched document into an event. Returns None when the
    document is missing or not T1-enriched (T2 requires T1 output)."""
    doc = conn.execute(
        "SELECT d.id, d.title,"
        " substr(COALESCE(d.published_at, d.fetched_at), 1, 10) AS doc_date,"
        " de.summary, de.event_type, de.model, de.prompt_version"
        " FROM document d JOIN document_enrichment de ON de.document_id = d.id"
        " WHERE d.id = ?", (document_id,)).fetchone()
    if doc is None:
        return None
    doc_vector = get_document_vector(conn, document_id)
    doc_entities = document_entity_ids(conn, document_id)
    candidates = find_candidates(
        conn, doc_date=doc["doc_date"], doc_vector=doc_vector,
        doc_entities=doc_entities, doc_event_type=doc["event_type"])
    best = candidates[0] if candidates else None

    if best is not None and best.score >= ATTACH_THRESHOLD:
        return await _attach(conn, provider, governor, doc=doc,
                             doc_vector=doc_vector, candidate=best,
                             method="attach", adjudication=None)
    if best is None or best.score <= NEW_EVENT_THRESHOLD:
        return _create_new(conn, doc=doc, doc_vector=doc_vector,
                           score=best.score if best else None,
                           method="new", adjudication=None)

    # -- gray zone: ONE FAST closed-menu adjudication -----------------------------
    menu = candidates[:TOP_K_ADJUDICATION]
    payload: dict = {
        "prompt_version": ADJ_PROMPT_VERSION,
        "menu": [{"event_id": c.event_id, "title": c.title,
                  "score": round(c.score, 4), "cosine": round(c.cosine, 4),
                  "entity_jaccard": round(c.entity_jaccard, 4),
                  "same_event_type": c.same_event_type} for c in menu],
    }
    choice: int | None = None
    if provider is None:
        payload["fallback"] = "no_provider"
    else:
        model = provider.model_for(ModelTier.FAST)
        try:
            governor.check(spend.cost_usd(
                model, input_tokens=EST_ADJ_INPUT_TOKENS,
                output_tokens=EST_ADJ_OUTPUT_TOKENS))
            completion = await provider.complete_structured(
                system=ADJ_SYSTEM,
                messages=[{"role": "user",
                           "content": _adjudication_message(doc, menu)}],
                schema=EventAdjudication, tier=ModelTier.FAST,
                max_tokens=128)
            spend.record_call(conn, purpose=PURPOSE_ADJUDICATION,
                              model=completion.model, usage=completion.usage)
            choice = completion.output.choice
            payload["choice"] = choice
            payload["model"] = completion.model
        except BudgetExceeded as e:
            log.warning("adjudication skipped (doc %s): %s", document_id, e)
            payload["fallback"] = "budget_exceeded"
        except LLMError as e:
            log.warning("adjudication failed (doc %s): %s", document_id, e)
            payload["fallback"] = "llm_error"

    if choice is not None and 1 <= choice <= len(menu):
        return await _attach(conn, provider, governor, doc=doc,
                             doc_vector=doc_vector,
                             candidate=menu[choice - 1],
                             method="adjudicated", adjudication=payload)
    method = "adjudicated" if choice is not None else "new"
    return _create_new(conn, doc=doc, doc_vector=doc_vector,
                       score=best.score, method=method, adjudication=payload)


def _adjudication_message(doc: sqlite3.Row, menu: list[Candidate]) -> str:
    lines = [
        "DOCUMENT:",
        f"  title: {doc['title'] or '(untitled)'}",
        f"  summary: {doc['summary']}",
        f"  event_type: {doc['event_type']}",
        f"  date: {doc['doc_date']}",
        "",
        "CANDIDATE EVENTS:",
    ]
    for i, c in enumerate(menu, start=1):
        lines.append(f"  {i}. [{c.event_type}] {c.title}"
                     f" ({c.occurred_on or 'undated'}; {c.doc_count} docs)")
        if c.description:
            lines.append(f"     {c.description}")
    lines.append("")
    lines.append("Which event does the document belong to?"
                 " choice = 1-%d, or 0 for NEW EVENT." % len(menu))
    return "\n".join(lines)


# -- attach / create ---------------------------------------------------------------

async def _attach(conn: sqlite3.Connection, provider: LLMProvider | None,
                  governor: Governor, *, doc: sqlite3.Row,
                  doc_vector: list[float] | None, candidate: Candidate,
                  method: str, adjudication: dict | None) -> AssignmentResult:
    now = utc_now()
    event_id = candidate.event_id
    with conn:
        cur = conn.execute(
            "INSERT INTO event_assignment (document_id, event_id, method,"
            " score, adjudication, created_at) VALUES (?,?,?,?,?,?)",
            (doc["id"], event_id, method, candidate.score,
             json.dumps(adjudication) if adjudication else None, now))
        assignment_id = int(cur.lastrowid)  # type: ignore[arg-type]
        # centroid running mean over the pre-attach member count
        if doc_vector is not None:
            centroid = get_event_centroid(conn, event_id)
            n = conn.execute("SELECT doc_count FROM event WHERE id = ?",
                             (event_id,)).fetchone()[0] or 0
            new_centroid = (update_centroid(centroid, n, doc_vector)
                            if centroid is not None and n > 0 else doc_vector)
            _set_event_centroid(conn, event_id, new_centroid, "centroid")
        conn.execute(
            "UPDATE event SET doc_count = doc_count + 1,"
            " last_seen_at = MAX(COALESCE(last_seen_at, ?), ?),"
            " window_start = MIN(COALESCE(window_start, ?), ?),"
            " window_end = MAX(COALESCE(window_end, ?), ?)"
            " WHERE id = ?",
            (doc["doc_date"], doc["doc_date"], doc["doc_date"],
             doc["doc_date"], doc["doc_date"], doc["doc_date"], event_id))
    refreshed = await _maybe_refresh_summary(conn, provider, governor,
                                             event_id)
    return AssignmentResult(assignment_id=assignment_id, event_id=event_id,
                            method=method, score=candidate.score,
                            created_event=False, summary_refreshed=refreshed)


def _create_new(conn: sqlite3.Connection, *, doc: sqlite3.Row,
                doc_vector: list[float] | None, score: float | None,
                method: str, adjudication: dict | None) -> AssignmentResult:
    now = utc_now()
    title = (doc["title"] or doc["summary"] or "Untitled event")[:200]
    with conn:
        cur = conn.execute(
            "INSERT INTO event (title, description, event_type, occurred_on,"
            " date_precision, doc_count, window_start, window_end,"
            " last_seen_at, grade, extractor_model, prompt_version,"
            " created_at) VALUES (?,?,?,?, 'day', 1, ?, ?, ?, 1, ?, ?, ?)",
            (title, doc["summary"], doc["event_type"], doc["doc_date"],
             doc["doc_date"], doc["doc_date"], doc["doc_date"],
             doc["model"], doc["prompt_version"], now))
        event_id = int(cur.lastrowid)  # type: ignore[arg-type]
        if doc_vector is not None:
            _set_event_centroid(conn, event_id, doc_vector, "centroid")
        cur = conn.execute(
            "INSERT INTO event_assignment (document_id, event_id, method,"
            " score, adjudication, created_at) VALUES (?,?,?,?,?,?)",
            (doc["id"], event_id, method, score,
             json.dumps(adjudication) if adjudication else None, now))
        assignment_id = int(cur.lastrowid)  # type: ignore[arg-type]
    return AssignmentResult(assignment_id=assignment_id, event_id=event_id,
                            method=method, score=score, created_event=True)


# -- title/summary refresh (log schedule: 3, 10, 25) -------------------------------

async def _maybe_refresh_summary(conn: sqlite3.Connection,
                                 provider: LLMProvider | None,
                                 governor: Governor,
                                 event_id: int) -> bool:
    """One FAST call when the cluster crosses a refresh size; non-fatal."""
    count = conn.execute("SELECT doc_count FROM event WHERE id = ?",
                         (event_id,)).fetchone()[0]
    if count not in SUMMARY_REFRESH_SIZES or provider is None:
        return False
    model = provider.model_for(ModelTier.FAST)
    try:
        governor.check(spend.cost_usd(
            model, input_tokens=EST_SUMMARY_INPUT_TOKENS,
            output_tokens=EST_SUMMARY_OUTPUT_TOKENS))
    except BudgetExceeded as e:
        log.warning("event summary refresh skipped (event %s): %s",
                    event_id, e)
        return False
    rows = conn.execute(
        "SELECT d.title, de.summary FROM event_assignment ea"
        " JOIN document d ON d.id = ea.document_id"
        " LEFT JOIN document_enrichment de ON de.document_id = d.id"
        " WHERE ea.event_id = ?"
        " ORDER BY COALESCE(d.published_at, d.fetched_at) DESC LIMIT ?",
        (event_id, SUMMARY_MAX_DOCS)).fetchall()
    lines = [f"- {r['title'] or '(untitled)'}: {r['summary'] or ''}"
             for r in rows]
    try:
        completion = await provider.complete_structured(
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user",
                       "content": "MEMBER DOCUMENTS:\n" + "\n".join(lines)}],
            schema=EventSummary, tier=ModelTier.FAST, max_tokens=512)
    except LLMError as e:
        log.warning("event summary refresh failed (event %s): %s",
                    event_id, e)
        return False
    spend.record_call(conn, purpose=PURPOSE_SUMMARY,
                      model=completion.model, usage=completion.usage)
    with conn:
        conn.execute("UPDATE event SET title = ?, description = ?"
                     " WHERE id = ?",
                     (completion.output.title, completion.output.summary,
                      event_id))
    return True
