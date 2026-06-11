"""AnalysisService — the sequential stage runner over the EXISTING
JobRunner (one 'analysis' job per analysis; job_event.seq is the SSE id).

An analysis IS a dossier row (the contract's analysis_id = dossier.id).
Each stage persists a dossier_section row (created_at = stage start,
updated_at = stage finish, content = the frozen stage output + summary), so
a failed stage marks the dossier failed cleanly and any later phase can
re-run from the last good section.

Event stream written per stage: stage_started{stage} ->
stage_progress{stage,message}* -> stage_completed{stage,summary}; verify
additionally emits claim_verified{claim_id,verdict}. Terminal events come
from the JobRunner ('done' on success, 'error'/'cancelled' otherwise) —
the SSE endpoint translates those to the contract shapes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

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
from connect.orchestration.jobs import JobRunner
from connect.retrieval.search_client import SearchClient
from connect.storage.db import utc_now

log = logging.getLogger(__name__)

STAGES = ("normalize", "verify", "assemble")


class AnalysisService:
    def __init__(self, conn: sqlite3.Connection, *, jobs: JobRunner,
                 provider: LLMProvider | None, governor: Governor,
                 search: SearchClient, ingest: IngestionPipeline | None,
                 embedder: Embedder, vectors: VectorIndex | None,
                 budget_usd: float = 0.75, default_max_evidence: int = 6):
        self.conn = conn
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

    def start(self, input_text: str, *,
              max_evidence_per_claim: int | None = None) -> tuple[int, int]:
        """Create the dossier + submit the job. Returns (analysis_id,
        job_id). Caller has validated input_text and provider presence."""
        k = max_evidence_per_claim or self.default_max_evidence
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO dossier (input_text, input_type, status,"
                " created_at) VALUES (?, 'claim', 'pending', ?)",
                (input_text, utc_now()))
        dossier_id = int(cur.lastrowid)  # type: ignore[arg-type]

        holder: dict[str, int] = {}

        async def _run():
            return await self._run_analysis(dossier_id, k, holder["job_id"])

        job_id = self.jobs.submit(
            "analysis",
            {"dossier_id": dossier_id, "max_evidence_per_claim": k}, _run)
        holder["job_id"] = job_id
        with self.conn:
            self.conn.execute("UPDATE job SET dossier_id = ? WHERE id = ?",
                              (dossier_id, job_id))
        return dossier_id, job_id

    def job_for(self, dossier_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM job WHERE dossier_id = ?"
            " ORDER BY id DESC LIMIT 1", (dossier_id,)).fetchone()
        return int(row[0]) if row else None

    def cancel(self, dossier_id: int) -> bool:
        """Flip the dossier to cancelled and cancel the job task (the
        in-task CancelledError handler re-sets the same state — idempotent;
        a task still queued on the semaphore never runs that handler, which
        is why the dossier is flipped here too). Returns False for
        unknown/already-terminal dossiers."""
        row = self.conn.execute(
            "SELECT status FROM dossier WHERE id = ?",
            (dossier_id,)).fetchone()
        if row is None or row["status"] not in ("pending", "running"):
            return False
        job_id = self.job_for(dossier_id)
        if job_id is not None:
            self.jobs.cancel_job(job_id)
        self._set_dossier(dossier_id, "cancelled", finished=True)
        return True

    # -- the job coroutine ------------------------------------------------------------

    async def _run_analysis(self, dossier_id: int, k: int,
                            job_id: int) -> None:
        if self.provider is None:
            self._set_dossier(dossier_id, "failed", finished=True,
                              error="ANTHROPIC_API_KEY not set")
            raise LLMError("LLM provider not configured "
                           "(ANTHROPIC_API_KEY not set)")
        row = self.conn.execute(
            "SELECT input_text FROM dossier WHERE id = ?",
            (dossier_id,)).fetchone()
        if row is None:
            raise ValueError(f"dossier {dossier_id} not found")
        input_text = row["input_text"]

        ctx = AnalysisContext(
            conn=self.conn, provider=self.provider, governor=self.governor,
            budget=AnalysisBudget(self.budget_usd), search=self.search,
            ingest=self.ingest, embedder=self.embedder,
            vectors=self.vectors, dossier_id=dossier_id,
            max_evidence_per_claim=k,
            emit=lambda type_, data: events.emit(
                self.conn, job_id, type_, data))

        self._set_dossier(dossier_id, "running", started=True)
        stage = "normalize"
        try:
            normalized = await self._stage_normalize(ctx, input_text)
            stage = "verify"
            claims = await self._stage_verify(ctx, normalized)
            stage = "assemble"
            self._stage_assemble(ctx, claims)
        except asyncio.CancelledError:
            self._section_finish(dossier_id, stage, "failed")
            self._set_dossier(dossier_id, "cancelled", finished=True)
            raise
        except Exception as e:
            self._section_finish(dossier_id, stage, "failed")
            self._set_dossier(dossier_id, "failed", finished=True,
                              error=str(e))
            raise
        self._set_dossier(dossier_id, "completed", finished=True)
        return None  # JobRunner's 'done' event carries empty data

    # -- stages ------------------------------------------------------------------------

    async def _stage_normalize(self, ctx: AnalysisContext,
                               input_text: str) -> NormalizedInput:
        self._stage_start(ctx, "normalize")
        normalized = await normalize.run(ctx, input_text)
        ctx.normalized = normalized
        summary = normalize.summarize(normalized)
        self._stage_complete(ctx, "normalize", summary, {
            "normalized": normalized.model_dump()})
        return normalized

    async def _stage_verify(self, ctx: AnalysisContext,
                            normalized: NormalizedInput,
                            ) -> list[ClaimResult]:
        self._stage_start(ctx, "verify")

        def _save(claims: list[ClaimResult], summary: str | None) -> None:
            self._section_content(ctx.dossier_id, "verify", {
                "summary": summary,
                "claims": [c.model_dump() for c in claims],
                "search": ctx.search.name,
            })

        claims, summary = await verify.run(ctx, normalized, save=_save)
        self._stage_complete(ctx, "verify", summary, {
            "claims": [c.model_dump() for c in claims],
            "search": ctx.search.name,
        })
        return claims

    def _stage_assemble(self, ctx: AnalysisContext,
                        claims: list[ClaimResult]) -> None:
        self._stage_start(ctx, "assemble")
        content, summary = assemble.run(claims, ctx.stage_summaries)
        self._stage_complete(ctx, "assemble", summary, content)

    # -- section / dossier row plumbing -----------------------------------------------------

    def _stage_start(self, ctx: AnalysisContext, stage: str) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at) VALUES (?,?, 'running', '{}', ?)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status='running', content='{}', created_at=excluded"
                ".created_at, updated_at=NULL",
                (ctx.dossier_id, stage, now))
            self.conn.execute(
                "UPDATE dossier SET current_stage = ? WHERE id = ?",
                (stage, ctx.dossier_id))
        ctx.emit("stage_started", {"stage": stage})

    def _stage_complete(self, ctx: AnalysisContext, stage: str,
                        summary: str, content: dict[str, Any]) -> None:
        ctx.stage_summaries[stage] = summary
        self._section_content(ctx.dossier_id, stage,
                              {"summary": summary, **content},
                              status="completed")
        ctx.emit("stage_completed", {"stage": stage, "summary": summary})

    def _section_content(self, dossier_id: int, stage: str,
                         content: dict[str, Any],
                         status: str | None = None) -> None:
        now = utc_now()
        with self.conn:
            if status is None:
                self.conn.execute(
                    "UPDATE dossier_section SET content = ?, updated_at = ?"
                    " WHERE dossier_id = ? AND stage = ?",
                    (json.dumps(content), now, dossier_id, stage))
            else:
                self.conn.execute(
                    "UPDATE dossier_section SET content = ?, status = ?,"
                    " updated_at = ? WHERE dossier_id = ? AND stage = ?",
                    (json.dumps(content), status, now, dossier_id, stage))

    def _section_finish(self, dossier_id: int, stage: str,
                        status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE dossier_section SET status = ?, updated_at = ?"
                " WHERE dossier_id = ? AND stage = ? AND status = 'running'",
                (status, utc_now(), dossier_id, stage))

    def _set_dossier(self, dossier_id: int, status: str, *,
                     started: bool = False, finished: bool = False,
                     error: str | None = None) -> None:
        sets, params = ["status = ?"], [status]
        if started:
            sets.append("started_at = ?")
            params.append(utc_now())
        if finished:
            sets.append("finished_at = ?")
            params.append(utc_now())
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        params.append(dossier_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE dossier SET {', '.join(sets)} WHERE id = ?",
                params)
