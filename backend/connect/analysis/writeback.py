"""KB writeback — analysis results land on CANONICAL claim nodes.

Reconciliation ladder (design result.pipeline §3, analysis-grade):
1. exact normalized-text match against existing claims -> merge;
2. embedding cosine over claim_embedding: >= 0.92 -> merge;
   0.80-0.92 -> ONE FAST same-proposition tie-break -> merge on yes;
3. else a NEW claim row (+ its embedding when available).

Evidence rows are written grade=2 (analyzed) and upsert on the
(claim_id, document_id) unique key — a newer analysis supersedes the prior
stance for the same pair, the row id stays stable. Verdict updates append
to verdict_history with an evidence-set snapshot so a dossier citing
"refuted as of <date>" stays auditable after the verdict flips.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from connect.analysis import grounding
from connect.analysis.context import (
    EST_ADJUDICATION_IN,
    EST_ADJUDICATION_OUT,
    AnalysisContext,
)
from connect.analysis.prompts import ANALYSIS_PROMPT_VERSION, SAME_PROPOSITION_SYSTEM
from connect.analysis.schema import ClaimKind, EvidenceRef, SameProposition
from connect.knowledge.linking.event_clusterer import cosine_similarity
from connect.knowledge.vector import _pack, _unpack
from connect.llm.provider import LLMError
from connect.llm.tiers import ModelTier
from connect.storage.db import utc_now

log = logging.getLogger(__name__)

MERGE_THRESHOLD = 0.92
ADJUDICATE_THRESHOLD = 0.80

# decomposition kind -> claim.claim_type (domain/enums.CLAIM_TYPES)
KIND_TO_CLAIM_TYPE: dict[str, str] = {
    "factual": "factual",
    "causal": "factual",
    "predictive": "prediction",
    "normative": "opinion",
}


def _claim_vector(ctx: AnalysisContext, text: str) -> list[float] | None:
    vectors = ctx.embedder.embed([text])
    return vectors[0] if vectors else None


def _nearest_claims(conn: sqlite3.Connection, vector: list[float],
                    k: int = 3) -> list[tuple[int, float]]:
    """Brute-force cosine over claim_embedding, best-first."""
    scored: list[tuple[int, float]] = []
    for row in conn.execute("SELECT claim_id, vector FROM claim_embedding"):
        sim = cosine_similarity(vector, _unpack(row["vector"]))
        scored.append((int(row["claim_id"]), sim))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:k]


async def reconcile_claim(ctx: AnalysisContext, *, text: str,
                          kind: ClaimKind) -> tuple[int, str]:
    """Resolve a decomposed claim onto a canonical claim row.

    Returns (claim_id, method) with method in
    'exact' | 'vec_merge' | 'adjudicated_merge' | 'new'.
    """
    conn = ctx.conn
    norm = grounding.norm_ws(text).lower()
    for row in conn.execute("SELECT id, text FROM claim"):
        if grounding.norm_ws(row["text"] or "").lower() == norm:
            return int(row["id"]), "exact"

    vector = _claim_vector(ctx, text)
    if vector is not None:
        for claim_id, sim in _nearest_claims(conn, vector):
            if sim >= MERGE_THRESHOLD:
                return claim_id, "vec_merge"
            if sim >= ADJUDICATE_THRESHOLD:
                if await _same_proposition(ctx, text, claim_id):
                    return claim_id, "adjudicated_merge"
            break  # only the best candidate is ever considered

    claim_id = _insert_claim(conn, text=text, kind=kind)
    if vector is not None:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO claim_embedding (claim_id, model,"
                " dim, vector) VALUES (?,?,?,?)",
                (claim_id, ctx.embedder.model_name, len(vector),
                 _pack(vector)))
    return claim_id, "new"


async def _same_proposition(ctx: AnalysisContext, text: str,
                            claim_id: int) -> bool:
    """Gray-zone tie-break; any failure (budget, provider) means 'no merge'
    — a duplicate claim node is recoverable, a wrong merge is not."""
    row = ctx.conn.execute("SELECT text FROM claim WHERE id = ?",
                           (claim_id,)).fetchone()
    if row is None:
        return False
    user = f"CLAIM A: {text}\nCLAIM B: {row['text']}\nSame proposition?"
    try:
        completion = await ctx.call_structured(
            system=SAME_PROPOSITION_SYSTEM, user=user,
            schema=SameProposition, tier=ModelTier.FAST, max_tokens=64,
            est_in=EST_ADJUDICATION_IN, est_out=EST_ADJUDICATION_OUT)
    except (LLMError, RuntimeError) as e:
        log.warning("same-proposition tie-break failed (claim %s): %s",
                    claim_id, e)
        return False
    return completion.output.same


def _insert_claim(conn: sqlite3.Connection, *, text: str,
                  kind: ClaimKind) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO claim (text, claim_type, created_at)"
            " VALUES (?,?,?)",
            (text, KIND_TO_CLAIM_TYPE.get(kind, "factual"), utc_now()))
    return int(cur.lastrowid)  # type: ignore[arg-type]


# -- evidence + verdict -----------------------------------------------------------


def write_evidence(conn: sqlite3.Connection, *, claim_id: int,
                   document_id: int, stance: str, relevance: float,
                   quote: str, note: str, model: str) -> int:
    """Upsert one analysis-grade evidence row; returns its (stable) id."""
    with conn:
        conn.execute(
            "INSERT INTO evidence (claim_id, document_id, stance,"
            " confidence, rationale, quote, method, model_id, grade,"
            " extractor_model, prompt_version, created_at)"
            " VALUES (?,?,?,?,?,?, 'llm', ?, 2, ?, ?, ?)"
            " ON CONFLICT (claim_id, document_id) DO UPDATE SET"
            " stance=excluded.stance, confidence=excluded.confidence,"
            " rationale=excluded.rationale, quote=excluded.quote,"
            " method='llm', model_id=excluded.model_id, grade=2,"
            " extractor_model=excluded.extractor_model,"
            " prompt_version=excluded.prompt_version,"
            " created_at=excluded.created_at",
            (claim_id, document_id, stance, relevance, note, quote, model,
             model, ANALYSIS_PROMPT_VERSION, utc_now()))
        row = conn.execute(
            "SELECT id FROM evidence WHERE claim_id = ? AND document_id = ?",
            (claim_id, document_id)).fetchone()
    return int(row[0])


def update_verdict(conn: sqlite3.Connection, *, claim_id: int, verdict: str,
                   confidence: float, dossier_id: int,
                   evidence: list[EvidenceRef]) -> None:
    """claim.verdict + verdict_history append (trigger 'analysis:<id>',
    evidence-set snapshot for audit)."""
    now = utc_now()
    snapshot: list[dict[str, Any]] = [
        {"evidence_id": e.evidence_id, "document_id": e.document_id,
         "stance": e.stance, "weight": round(e.weight, 4)}
        for e in evidence]
    with conn:
        conn.execute(
            "UPDATE claim SET verdict = ?, confidence = ?,"
            " verdict_updated_at = ? WHERE id = ?",
            (verdict, confidence, now, claim_id))
        conn.execute(
            'INSERT INTO verdict_history (claim_id, verdict, computed_at,'
            ' "trigger", evidence_snapshot) VALUES (?,?,?,?,?)',
            (claim_id, verdict, now, f"analysis:{dossier_id}",
             json.dumps(snapshot)))
