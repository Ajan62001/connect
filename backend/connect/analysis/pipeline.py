"""AnalysisService — the sequential stage runner (one 'analysis' job per
analysis; job_event.seq is the SSE id). start() persists the dossier row
and enqueues a self-describing payload; the registered 'analysis' handler
(workers/handlers/analysis.py) drives run().

An analysis IS a dossier row (the contract's analysis_id = dossier.id).
Each stage persists a dossier_section row (created_at = stage start,
updated_at = stage finish, content = the frozen stage output + summary), so
a failed stage marks the dossier failed cleanly and any later phase can
re-run from the last good section.

v0.2: the service holds the POOL; short units of work (start/cancel/
job_for) acquire their own connection, while run() — the job body — uses
the JOB's connection handed in by the worker (one connection per job).

Event stream written per stage: stage_started{stage} ->
stage_progress{stage,message}* -> stage_completed{stage,summary}; verify
additionally emits claim_verified{claim_id,verdict}. Terminal events come
from the queue's job execution ('done' on success, 'error'/'cancelled'
otherwise) — the SSE endpoint translates those to the contract shapes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.analysis.budget import AnalysisBudget
from connect.analysis.context import AnalysisContext
from connect.analysis.schema import ClaimResult, NormalizedInput
from connect.analysis.stages import assemble, normalize, verify
from connect.ingestion.pipeline import IngestionPipeline
from connect.knowledge.embedder import Embedder
from connect.knowledge.vector import VectorIndex
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import Governor
from connect.orchestration import events
from connect.retrieval.search_client import SearchClient
from connect.storage.pg import Jsonb, utc_now
from connect.workers.queue import JobQueue
from connect.workers.registry import CancelToken

log = logging.getLogger(__name__)

STAGES = ("normalize", "verify", "assemble")


class AnalysisService:
    def __init__(self, pool: AsyncConnectionPool, *, jobs: JobQueue,
                 provider: LLMProvider | None, governor: Governor,
                 search: SearchClient, ingest: IngestionPipeline | None,
                 embedder: Embedder, vectors: VectorIndex | None,
                 budget_usd: float = 0.75, default_max_evidence: int = 6):
        self.pool = pool
        self.jobs = jobs
        self.provider = provider
        self.governor = governor
        self.search = search
        self.ingest = ingest
        self.embedder = embedder
        self.vectors = vectors
        self.budget_usd = budget_usd
        self.default_max_evidence = default_max_evidence

    # -- start / cancel -----------------------------------------------------------

    async def start(self, input_text: str, *,
                    max_evidence_per_claim: int | None = None,
                    ) -> tuple[int, int]:
        """Create the dossier + enqueue the job (the registered 'analysis'
        handler reconstructs the run from the payload). Returns
        (analysis_id, job_id). Caller has validated input_text and
        provider presence."""
        k = max_evidence_per_claim or self.default_max_evidence
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO dossier (input_text, input_type, status,"
                    " created_at) VALUES (%s, 'claim', 'pending', %s)"
                    " RETURNING id",
                    (input_text, utc_now()))
                dossier_id = int((await cur.fetchone())["id"])
        job_id = await self.jobs.enqueue(
            "analysis",
            {"dossier_id": dossier_id, "max_evidence_per_claim": k},
            dossier_id=dossier_id)
        return dossier_id, job_id

    async def job_for(self, dossier_id: int) -> int | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM job WHERE dossier_id = %s"
                " ORDER BY id DESC LIMIT 1", (dossier_id,))
            row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def cancel(self, dossier_id: int) -> bool:
        """Flip the dossier to cancelled and request job cancellation (the
        in-job CancelledError handler re-sets the same state — idempotent;
        a still-queued job never runs that handler, which is why the
        dossier is flipped here too). Returns False for unknown/
        already-terminal dossiers."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status FROM dossier WHERE id = %s",
                (dossier_id,))
            row = await cur.fetchone()
            if row is None or row["status"] not in ("pending", "running"):
                return False
            job_id = await self.job_for(dossier_id)
            if job_id is not None:
                await self.jobs.request_cancel(job_id)
            await self._set_dossier(conn, dossier_id, "cancelled",
                                    finished=True)
        return True

    # -- the job body (driven by the registered 'analysis' handler) ---------------------

    async def run(self, conn: psycopg.AsyncConnection, dossier_id: int,
                  max_evidence_per_claim: int | None, *, job_id: int,
                  cancel: CancelToken | None = None) -> None:
        k = max_evidence_per_claim or self.default_max_evidence
        if self.provider is None:
            await self._set_dossier(conn, dossier_id, "failed",
                                    finished=True,
                                    error="ANTHROPIC_API_KEY not set")
            raise LLMError("LLM provider not configured "
                           "(ANTHROPIC_API_KEY not set)")
        cur = await conn.execute(
            "SELECT input_text FROM dossier WHERE id = %s",
            (dossier_id,))
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"dossier {dossier_id} not found")
        input_text = row["input_text"]

        async def _emit(type_: str, data: dict[str, Any]) -> int:
            return await events.emit(conn, job_id, type_, data)

        ctx = AnalysisContext(
            conn=conn, provider=self.provider, governor=self.governor,
            budget=AnalysisBudget(self.budget_usd), search=self.search,
            ingest=self.ingest, embedder=self.embedder,
            vectors=self.vectors, dossier_id=dossier_id,
            max_evidence_per_claim=k,
            emit=_emit)

        await self._set_dossier(conn, dossier_id, "running", started=True)
        stage = "normalize"
        try:
            # stage boundaries are the cooperative-cancel checkpoints
            if cancel is not None:
                await cancel.raise_if_cancelled()
            normalized = await self._stage_normalize(ctx, input_text)
            stage = "verify"
            if cancel is not None:
                await cancel.raise_if_cancelled()
            claims = await self._stage_verify(ctx, normalized)
            stage = "assemble"
            if cancel is not None:
                await cancel.raise_if_cancelled()
            await self._stage_assemble(ctx, claims)
        except asyncio.CancelledError:
            await self._section_finish(conn, dossier_id, stage, "failed")
            await self._set_dossier(conn, dossier_id, "cancelled",
                                    finished=True)
            raise
        except Exception as e:
            await self._section_finish(conn, dossier_id, stage, "failed")
            await self._set_dossier(conn, dossier_id, "failed",
                                    finished=True, error=str(e))
            raise
        await self._set_dossier(conn, dossier_id, "completed", finished=True)
        return None  # the queue's 'done' event carries empty data

    # -- stages ------------------------------------------------------------------------

    async def _stage_normalize(self, ctx: AnalysisContext,
                               input_text: str) -> NormalizedInput:
        await self._stage_start(ctx, "normalize")
        normalized = await normalize.run(ctx, input_text)
        ctx.normalized = normalized
        summary = normalize.summarize(normalized)
        await self._stage_complete(ctx, "normalize", summary, {
            "normalized": normalized.model_dump()})
        return normalized

    async def _stage_verify(self, ctx: AnalysisContext,
                            normalized: NormalizedInput,
                            ) -> list[ClaimResult]:
        await self._stage_start(ctx, "verify")

        async def _save(claims: list[ClaimResult],
                        summary: str | None) -> None:
            await self._section_content(ctx.conn, ctx.dossier_id, "verify", {
                "summary": summary,
                "claims": [c.model_dump() for c in claims],
                "search": ctx.search.name,
            })

        claims, summary = await verify.run(ctx, normalized, save=_save)
        await self._stage_complete(ctx, "verify", summary, {
            "claims": [c.model_dump() for c in claims],
            "search": ctx.search.name,
        })
        return claims

    async def _stage_assemble(self, ctx: AnalysisContext,
                              claims: list[ClaimResult]) -> None:
        await self._stage_start(ctx, "assemble")
        content, summary = assemble.run(claims, ctx.stage_summaries)
        await self._stage_complete(ctx, "assemble", summary, content)

    # -- section / dossier row plumbing -----------------------------------------------------

    async def _stage_start(self, ctx: AnalysisContext, stage: str) -> None:
        now = utc_now()
        async with ctx.conn.transaction():
            await ctx.conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at) VALUES (%s,%s, 'running', '{}', %s)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status='running', content='{}'::jsonb,"
                " created_at=EXCLUDED.created_at, updated_at=NULL",
                (ctx.dossier_id, stage, now))
            await ctx.conn.execute(
                "UPDATE dossier SET current_stage = %s WHERE id = %s",
                (stage, ctx.dossier_id))
        await ctx.emit("stage_started", {"stage": stage})

    async def _stage_complete(self, ctx: AnalysisContext, stage: str,
                              summary: str, content: dict[str, Any]) -> None:
        ctx.stage_summaries[stage] = summary
        await self._section_content(ctx.conn, ctx.dossier_id, stage,
                                    {"summary": summary, **content},
                                    status="completed")
        await ctx.emit("stage_completed", {"stage": stage,
                                           "summary": summary})

    async def _section_content(self, conn: psycopg.AsyncConnection,
                               dossier_id: int, stage: str,
                               content: dict[str, Any],
                               status: str | None = None) -> None:
        now = utc_now()
        async with conn.transaction():
            if status is None:
                await conn.execute(
                    "UPDATE dossier_section SET content = %s,"
                    " updated_at = %s WHERE dossier_id = %s AND stage = %s",
                    (Jsonb(content), now, dossier_id, stage))
            else:
                await conn.execute(
                    "UPDATE dossier_section SET content = %s, status = %s,"
                    " updated_at = %s WHERE dossier_id = %s AND stage = %s",
                    (Jsonb(content), status, now, dossier_id, stage))

    async def _section_finish(self, conn: psycopg.AsyncConnection,
                              dossier_id: int, stage: str,
                              status: str) -> None:
        async with conn.transaction():
            await conn.execute(
                "UPDATE dossier_section SET status = %s, updated_at = %s"
                " WHERE dossier_id = %s AND stage = %s"
                " AND status = 'running'",
                (status, utc_now(), dossier_id, stage))

    async def _set_dossier(self, conn: psycopg.AsyncConnection,
                           dossier_id: int, status: str, *,
                           started: bool = False, finished: bool = False,
                           error: str | None = None) -> None:
        sets, params = ["status = %s"], [status]
        if started:
            sets.append("started_at = %s")
            params.append(utc_now())
        if finished:
            sets.append("finished_at = %s")
            params.append(utc_now())
        if error is not None:
            sets.append("error = %s")
            params.append(error)
        params.append(dossier_id)
        async with conn.transaction():
            await conn.execute(
                f"UPDATE dossier SET {', '.join(sets)} WHERE id = %s",
                params)
