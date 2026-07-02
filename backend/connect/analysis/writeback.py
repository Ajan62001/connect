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

import logging
from typing import Any

import psycopg

from connect.analysis import grounding
from connect.analysis.context import (
    EST_ADJUDICATION_IN,
    EST_ADJUDICATION_OUT,
    AnalysisContext,
)
from connect.analysis.prompts import ANALYSIS_PROMPT_VERSION, SAME_PROPOSITION_SYSTEM
from connect.analysis.schema import ClaimKind, EvidenceRef, SameProposition
from connect.integrity import observe as integrity_observe
from connect.integrity.signals import IntegritySignal
from connect.knowledge import corrections
from connect.llm.provider import LLMError
from connect.llm.tiers import ModelTier
from connect.storage.pg import Jsonb, Vector, utc_now

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


async def _nearest_claims(conn: psycopg.AsyncConnection,
                          vector: list[float],
                          k: int = 3) -> list[tuple[int, float]]:
    """Cosine nearest claims over claim_embedding, best-first — SQL-side
    pgvector KNN (P3 rewrite of the fetch-everything Python loop). The
    claim_id tie-break matches the v0.1 sort exactly and keeps the scan
    exact (a deliberate parity pick over approximate HNSW ordering — drop
    the tie-break to let idx_claim_embedding_hnsw serve this at scale)."""
    probe = Vector(vector)
    cur = await conn.execute(
        "SELECT claim_id, 1 - (embedding <=> %s) AS sim"
        " FROM claim_embedding"
        " ORDER BY embedding <=> %s, claim_id LIMIT %s",
        (probe, probe, k))
    return [(int(r["claim_id"]), float(r["sim"]))
            for r in await cur.fetchall()]


async def reconcile_claim(ctx: AnalysisContext, *, text: str,
                          kind: ClaimKind) -> tuple[int, str]:
    """Resolve a decomposed claim onto a canonical claim row.

    Returns (claim_id, method) with method in
    'exact' | 'vec_merge' | 'adjudicated_merge' | 'new'.
    """
    conn = ctx.conn
    norm = grounding.norm_ws(text).lower()
    cur = await conn.execute("SELECT id, text FROM claim")
    for row in await cur.fetchall():
        if grounding.norm_ws(row["text"] or "").lower() == norm:
            return int(row["id"]), "exact"

    vector = _claim_vector(ctx, text)
    if vector is not None:
        for claim_id, sim in await _nearest_claims(conn, vector):
            if sim >= MERGE_THRESHOLD:
                return claim_id, "vec_merge"
            if sim >= ADJUDICATE_THRESHOLD:
                if await _same_proposition(ctx, text, claim_id):
                    return claim_id, "adjudicated_merge"
            break  # only the best candidate is ever considered

    claim_id = await _insert_claim(conn, text=text, kind=kind)
    if vector is not None:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO claim_embedding (claim_id, model, embedding)"
                " VALUES (%s,%s,%s)"
                " ON CONFLICT (claim_id) DO UPDATE SET"
                " model = EXCLUDED.model, embedding = EXCLUDED.embedding",
                (claim_id, ctx.embedder.model_name, Vector(vector)))
    return claim_id, "new"


async def _same_proposition(ctx: AnalysisContext, text: str,
                            claim_id: int) -> bool:
    """Gray-zone tie-break; any failure (budget, provider) means 'no merge'
    — a duplicate claim node is recoverable, a wrong merge is not."""
    cur = await ctx.conn.execute("SELECT text FROM claim WHERE id = %s",
                                 (claim_id,))
    row = await cur.fetchone()
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


async def _insert_claim(conn: psycopg.AsyncConnection, *, text: str,
                        kind: ClaimKind) -> int:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO claim (text, claim_type, created_at)"
            " VALUES (%s,%s,%s) RETURNING id",
            (text, KIND_TO_CLAIM_TYPE.get(kind, "factual"), utc_now()))
        row = await cur.fetchone()
    return int(row["id"])


# -- evidence + verdict -----------------------------------------------------------


async def write_evidence(conn: psycopg.AsyncConnection, *, claim_id: int,
                         document_id: int, stance: str, relevance: float,
                         quote: str, note: str, model: str) -> int:
    """Upsert one analysis-grade evidence row; returns its (stable) id."""
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO evidence (claim_id, document_id, stance,"
            " confidence, rationale, quote, method, model_id, grade,"
            " extractor_model, prompt_version, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s, 'llm', %s, 2, %s, %s, %s)"
            " ON CONFLICT (claim_id, document_id) DO UPDATE SET"
            " stance=EXCLUDED.stance, confidence=EXCLUDED.confidence,"
            " rationale=EXCLUDED.rationale, quote=EXCLUDED.quote,"
            " method='llm', model_id=EXCLUDED.model_id, grade=2,"
            " extractor_model=EXCLUDED.extractor_model,"
            " prompt_version=EXCLUDED.prompt_version,"
            " created_at=EXCLUDED.created_at"
            " RETURNING id",
            (claim_id, document_id, stance, relevance, note, quote, model,
             model, ANALYSIS_PROMPT_VERSION, utc_now()))
        row = await cur.fetchone()
    return int(row["id"])


async def update_verdict(conn: psycopg.AsyncConnection, *, claim_id: int,
                         verdict: str, confidence: float, dossier_id: int,
                         evidence: list[EvidenceRef]) -> None:
    """claim.verdict + verdict_history append (trigger 'analysis:<id>',
    evidence-set snapshot for audit)."""
    now = utc_now()
    snapshot: list[dict[str, Any]] = [
        {"evidence_id": e.evidence_id, "document_id": e.document_id,
         "stance": e.stance, "weight": round(e.weight, 4)}
        for e in evidence]
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT verdict FROM claim WHERE id = %s", (claim_id,))
        prior_row = await cur.fetchone()
        prior = prior_row["verdict"] if prior_row else None
        await conn.execute(
            "UPDATE claim SET verdict = %s, confidence = %s,"
            " verdict_updated_at = %s WHERE id = %s",
            (verdict, confidence, now, claim_id))
        await conn.execute(
            'INSERT INTO verdict_history (claim_id, verdict, computed_at,'
            ' "trigger", evidence_snapshot) VALUES (%s,%s,%s,%s,%s)',
            (claim_id, verdict, now, f"analysis:{dossier_id}",
             Jsonb(snapshot)))
        # S3: a real verdict flip into/out of refuted/mixed opens a correction
        # that the content_correction sweep fans out to dependent published items
        if corrections.is_propagating_flip(prior, verdict):
            await corrections.record_verdict_flip(
                conn, claim_id=claim_id, from_verdict=prior, to_verdict=verdict)
        # S5: verdict-distribution telemetry (best-effort)
        await integrity_observe.record(conn, IntegritySignal(
            kind="verdict", surface="analysis", subject_id=claim_id,
            value=verdict))
