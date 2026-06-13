"""InvestigationService — dossier lifecycle + the MANUAL tool loop
(design §2.3). One 'investigation' job per run; start() enqueues a
self-describing payload (seed + options) and the registered handler
(workers/handlers/investigation.py) drives run(). job_event.seq is the
SSE id, exactly the Phase-3 analyses contract.

The loop drives ``complete_with_tools`` directly (NOT the generic
tool_loop template) because every turn is individually metered: ledger row
(purpose 'investigation'), per-run budget debit, degrade-ladder evaluation
(60% -> corpus-only, 85% -> forced conclude, $0.20 synthesis reserve) and
an 'iteration' SSE event. Durable state is the finding/question/edge rows
plus the job_event log — a crash loses nothing already recorded, and
synthesize is re-runnable from rows alone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.agents.metered_loop import run_metered_tool_loop
from connect.analysis.budget import AnalysisBudget
from connect.ingestion.pipeline import IngestionPipeline
from connect.investigation import questions as questions_mod
from connect.investigation import scoping, synthesize
from connect.investigation.prompts import INVESTIGATOR_SYSTEM
from connect.investigation.schema import (
    InvestigationOptions,
    InvestigationSeed,
    ScopePack,
)
from connect.investigation.tools import (
    TOOL_DEFS,
    InvestigationState,
    ToolExecutor,
)
from connect.knowledge.embedder import Embedder
from connect.knowledge.enrichment import persist as t1_persist
from connect.knowledge.enrichment import t1 as t1_mod
from connect.knowledge.vector import VectorIndex
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.orchestration import events
from connect.retrieval.search_client import SearchClient
from connect.sources.seeds import WEB_INVESTIGATION_SOURCE
from connect.storage.pg import Jsonb, utc_now
from connect.workers.queue import JobQueue
from connect.workers.registry import CancelToken

log = logging.getLogger(__name__)

PURPOSE_INVESTIGATION = "investigation"
PURPOSE_INVESTIGATION_T1 = "investigation_t1"

# per-turn projection for governor/budget look-ahead (cached system+tools
# re-read ~0.1x; projected conservatively at full rate)
EST_LOOP_IN, EST_LOOP_OUT = 6000, 700
LOOP_MAX_TOKENS = 2048

# the degrade ladder (fractions of the per-run budget)
CORPUS_ONLY_FRACTION = 0.60
FORCE_CONCLUDE_FRACTION = 0.85


class InvestigationService:
    def __init__(self, pool: AsyncConnectionPool, *, jobs: JobQueue,
                 provider: LLMProvider | None, governor: Governor,
                 search: SearchClient, ingest: IngestionPipeline | None,
                 embedder: Embedder, vectors: VectorIndex | None,
                 default_budget_usd: float = 1.00,
                 default_max_iterations: int = 14,
                 default_max_web_fetches: int = 8,
                 synthesis_reserve_usd: float = 0.20,
                 synthesis_tier: str = "deep"):
        self.pool = pool
        self.jobs = jobs
        self.provider = provider
        self.governor = governor          # the INVESTIGATION governor
        self.search = search
        self.ingest = ingest
        self.embedder = embedder
        self.vectors = vectors
        self.default_budget_usd = default_budget_usd
        self.default_max_iterations = default_max_iterations
        self.default_max_web_fetches = default_max_web_fetches
        self.synthesis_reserve_usd = synthesis_reserve_usd
        self.synthesis_tier = synthesis_tier

    # -- start / cancel ------------------------------------------------------------

    def default_options(self) -> InvestigationOptions:
        return InvestigationOptions(
            budget_usd=self.default_budget_usd,
            max_iterations=self.default_max_iterations,
            max_web_fetches=self.default_max_web_fetches)

    async def start(self, seed: InvestigationSeed,
                    options: InvestigationOptions | None = None, *,
                    owner_id: int,
                    visibility: str | None = None) -> tuple[int, int]:
        """Create dossier(kind='investigation') + enqueue the job (the
        payload is self-describing: the registered handler reconstructs
        seed + options from it); returns (investigation_id, job_id).
        Raises LookupError when the seed references a missing row.

        Visibility (design §1): None -> 'shared' (community compounding),
        EXCEPT when the seed is a question of a PRIVATE dossier — the
        child inherits 'private' (its scope descends from private work)."""
        opts = options or self.default_options()
        async with self.pool.connection() as conn:
            input_text, input_type, parent_question_id = \
                await scoping.resolve_seed(conn, seed)
            if visibility is None:
                visibility = "shared"
                if parent_question_id is not None:
                    cur = await conn.execute(
                        "SELECT d.visibility FROM question q"
                        " JOIN dossier d ON d.id = q.dossier_id"
                        " WHERE q.id = %s", (parent_question_id,))
                    parent = await cur.fetchone()
                    if parent is not None:
                        visibility = parent["visibility"]
            async with conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO dossier (kind, input_text, input_type,"
                    " parent_question_id, budget_usd, status, created_at,"
                    " owner_id, visibility)"
                    " VALUES ('investigation', %s, %s, %s, %s, 'pending',"
                    " %s, %s, %s) RETURNING id",
                    (input_text, input_type, parent_question_id,
                     opts.budget_usd, utc_now(), owner_id, visibility))
                dossier_id = int((await cur.fetchone())["id"])
                if parent_question_id is not None:
                    await conn.execute(
                        "UPDATE question SET spawned_dossier_id = %s,"
                        " updated_at = %s WHERE id = %s",
                        (dossier_id, utc_now(), parent_question_id))
        job_id = await self.jobs.enqueue(
            "investigation",
            {"dossier_id": dossier_id, "seed": seed.model_dump(),
             "options": opts.model_dump()},
            dossier_id=dossier_id, owner_id=owner_id)
        return dossier_id, job_id

    async def job_for(self, dossier_id: int) -> int | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM job WHERE dossier_id = %s"
                " ORDER BY id DESC LIMIT 1", (dossier_id,))
            row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def cancel(self, dossier_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status FROM dossier WHERE id = %s"
                " AND kind = 'investigation'", (dossier_id,))
            row = await cur.fetchone()
            if row is None or row["status"] not in ("pending", "running"):
                return False
            job_id = await self.job_for(dossier_id)
            if job_id is not None:
                await self.jobs.request_cancel(job_id)
            await self._set_dossier(conn, dossier_id, "cancelled",
                                    finished=True)
        return True

    # -- the job body (driven by the registered 'investigation' handler) --------------

    async def run(self, conn: psycopg.AsyncConnection, dossier_id: int,
                  seed: InvestigationSeed,
                  opts: InvestigationOptions, *, job_id: int,
                  cancel: CancelToken | None = None) -> None:
        if self.provider is None:
            await self._set_dossier(conn, dossier_id, "failed",
                                    finished=True,
                                    error="ANTHROPIC_API_KEY not set")
            raise LLMError("LLM provider not configured "
                           "(ANTHROPIC_API_KEY not set)")

        async def emit(type_: str,
                       data: dict[str, Any] | None = None) -> int:
            return await events.emit(conn, job_id, type_, data)

        await self._set_dossier(conn, dossier_id, "running", started=True)
        budget = AnalysisBudget(opts.budget_usd)
        stage = "scope"
        try:
            # stage boundaries + loop iterations are the cooperative-cancel
            # checkpoints
            if cancel is not None:
                await cancel.raise_if_cancelled()
            pack = await self._stage_scope(conn, dossier_id, seed, emit)
            stage = "investigate"
            if cancel is not None:
                await cancel.raise_if_cancelled()
            state = await self._stage_investigate(
                conn, dossier_id, pack, opts, budget, emit, cancel=cancel)
            stage = "synthesize"
            if cancel is not None:
                await cancel.raise_if_cancelled()
            await self._stage_synthesize(
                conn, dossier_id, pack, budget, state, emit)
        except asyncio.CancelledError:
            await self._section_finish(conn, dossier_id, stage, "failed")
            await self._set_dossier(conn, dossier_id, "cancelled",
                                    finished=True)
            await self._save_usage(conn, dossier_id, budget, None)
            raise
        except Exception as e:
            await self._section_finish(conn, dossier_id, stage, "failed")
            await self._set_dossier(conn, dossier_id, "failed",
                                    finished=True, error=str(e))
            await self._save_usage(conn, dossier_id, budget, None)
            raise
        await self._set_dossier(conn, dossier_id, "completed",
                                finished=True)
        return None  # the queue's 'done' event carries empty data

    # -- stage: scope (zero LLM) -------------------------------------------------------

    async def _stage_scope(self, conn: psycopg.AsyncConnection,
                           dossier_id: int, seed: InvestigationSeed,
                           emit) -> ScopePack:
        await self._section_start(conn, dossier_id, "scope")
        cur = await conn.execute(
            "SELECT input_text, input_type, owner_id FROM dossier"
            " WHERE id = %s",
            (dossier_id,))
        row = await cur.fetchone()
        pack = await scoping.build_scope_pack(
            conn, embedder=self.embedder, vectors=self.vectors,
            input_text=row["input_text"], input_type=row["input_type"],
            seed=seed, viewer=row["owner_id"])
        summary = (f"{len(pack.documents)} docs, {len(pack.anchors)}"
                   f" anchors, {len(pack.timeline)} timeline items,"
                   f" {len(pack.reaction_candidates)} reaction candidates,"
                   f" coverage {pack.coverage}")
        await self._section_complete(conn, dossier_id, "scope",
                                     {"summary": summary,
                                      **pack.model_dump()})
        await emit("section_completed", {"section": "scope",
                                         "summary": summary})
        return pack

    # -- stage: investigate (the manual loop) --------------------------------------------

    async def _stage_investigate(self, conn: psycopg.AsyncConnection,
                                 dossier_id: int, pack: ScopePack,
                                 opts: InvestigationOptions,
                                 budget: AnalysisBudget, emit,
                                 cancel: CancelToken | None = None,
                                 ) -> InvestigationState:
        assert self.provider is not None
        await self._section_start(conn, dossier_id, "investigate")
        await questions_mod.generate_questions(
            conn, self.provider, dossier_id=dossier_id, pack=pack,
            governor=self.governor, budget=budget, emit=emit)

        state = InvestigationState(
            dossier_id=dossier_id, max_web_fetches=opts.max_web_fetches)
        cur = await conn.execute(
            "SELECT owner_id FROM dossier WHERE id = %s", (dossier_id,))
        owner_row = await cur.fetchone()
        executor = ToolExecutor(
            conn, pipeline=self.ingest, search=self.search,
            embedder=self.embedder, vectors=self.vectors, scope_pack=pack,
            state=state, emit=emit,
            web_source_id=await self._web_source_id(conn),
            t1_enrich=self._make_t1_enricher(conn, budget),
            viewer=owner_row["owner_id"] if owner_row else None)

        model = self.provider.model_for(ModelTier.BALANCED)
        turn_proj = spend.cost_usd(model, input_tokens=EST_LOOP_IN,
                                   output_tokens=EST_LOOP_OUT)
        loop_cap = max(0.0, opts.budget_usd - self.synthesis_reserve_usd)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": scoping.render_scope_pack(pack)
            + await questions_mod.render_questions(conn, dossier_id)}]

        # The control flow is the shared metered tool-loop; the investigation
        # POLICY (3-tier degrade ladder, per-iteration SSE + usage save, the
        # end_turn->force-conclude nudge, raise-on-cancel) lives in these hooks.
        loop_state = {"force_next": False}

        async def should_force(n: int) -> bool:
            spent = budget.spent_usd
            if (not state.corpus_only
                    and spent >= CORPUS_ONLY_FRACTION * opts.budget_usd):
                state.corpus_only = True
            force = (loop_state["force_next"] or n == opts.max_iterations
                     or spent >= FORCE_CONCLUDE_FRACTION * opts.budget_usd
                     or spent + turn_proj > loop_cap)
            if not force:
                try:
                    await self.governor.check(turn_proj)
                except BudgetExceeded:
                    force = True  # one last forced-conclude call
            return force

        async def after_turn(n: int, turn: Any, forced: bool) -> None:
            await spend.record_call(conn, purpose=PURPOSE_INVESTIGATION,
                                    model=turn.model, usage=turn.usage)
            budget.add(spend.cost_usd(
                turn.model, input_tokens=turn.usage.input_tokens,
                output_tokens=turn.usage.output_tokens,
                cache_read_tokens=turn.usage.cache_read_tokens))
            await emit("iteration", {
                "n": n, "tools": [c.name for c in turn.tool_calls],
                "cost_so_far": round(budget.spent_usd, 4),
                "forced": forced, "corpus_only": state.corpus_only})
            await self._save_usage(conn, dossier_id, budget, state)

        async def on_empty_turn(turn: Any) -> bool:
            # end_turn without tools -> forced conclude next turn
            messages.append({
                "role": "user",
                "content": "You must finish now: call the conclude"
                           " tool with honest resolutions for every"
                           " question."})
            loop_state["force_next"] = True
            return False

        async def cancelled() -> bool:
            if cancel is not None:  # iteration = cancel checkpoint
                await cancel.raise_if_cancelled()
            return False

        iterations = await run_metered_tool_loop(
            self.provider, system=INVESTIGATOR_SYSTEM, tools=TOOL_DEFS,
            executor=executor, messages=messages, tier=ModelTier.BALANCED,
            max_tokens=LOOP_MAX_TOKENS, max_iters=opts.max_iterations,
            terminal_tool="conclude", should_force=should_force,
            after_turn=after_turn, on_empty_turn=on_empty_turn,
            is_concluded=lambda: state.concluded, should_cancel=cancelled)

        summary = (f"{iterations} iterations,"
                   f" {state.findings_recorded} findings,"
                   f" {state.questions_raised} questions raised,"
                   f" {state.docs_added} docs added,"
                   f" ${budget.spent_usd:.4f} spent"
                   + ("" if state.concluded else "; loop ended without"
                                                 " conclude"))
        await self._section_complete(conn, dossier_id, "investigate", {
            "summary": summary,
            "iterations": iterations,
            "concluded": state.concluded,
            "conclude_summary": state.conclude_summary,
            "conclude_confidence": state.conclude_confidence,
            "findings_recorded": state.findings_recorded,
            "rejected_findings": state.rejected_findings,
            "docs_added": state.docs_added,
            "web_fetches_used": state.web_fetches_used,
            "corpus_only": state.corpus_only,
            "cost_usd": round(budget.spent_usd, 4),
        })
        await emit("section_completed", {"section": "investigate",
                                         "summary": summary})
        return state

    # -- stage: synthesize ---------------------------------------------------------------

    async def _stage_synthesize(self, conn: psycopg.AsyncConnection,
                                dossier_id: int, pack: ScopePack,
                                budget: AnalysisBudget,
                                state: InvestigationState, emit) -> None:
        assert self.provider is not None
        await self._section_start(conn, dossier_id, "synthesize")

        async def _writer(d_id, stage, content, status="completed"):
            await self._write_section(conn, d_id, stage, content,
                                      status=status)

        summary = await synthesize.run(
            conn, self.provider, dossier_id=dossier_id, pack=pack,
            budget=budget, governor=self.governor, emit=emit,
            tier_pref=self.synthesis_tier,
            reserve_usd=self.synthesis_reserve_usd,
            section_writer=_writer)
        await self._section_complete(conn, dossier_id, "synthesize",
                                     {"summary": summary})
        await emit("section_completed", {"section": "synthesize",
                                         "summary": summary})
        await self._save_usage(conn, dossier_id, budget, state)

    # -- plumbing -----------------------------------------------------------------------

    async def _web_source_id(self,
                             conn: psycopg.AsyncConnection) -> int | None:
        cur = await conn.execute(
            "SELECT id FROM source WHERE name = %s",
            (WEB_INVESTIGATION_SOURCE,))
        row = await cur.fetchone()
        return int(row["id"]) if row else None

    def _make_t1_enricher(self, conn: psycopg.AsyncConnection,
                          budget: AnalysisBudget):
        """Synchronous T1 on investigation-fetched docs, ledgered under
        'investigation_t1' and debited against the per-run budget. Never
        raises into the fetch tool."""
        provider = self.provider

        async def _enrich(document_id: int) -> bool:
            if provider is None:
                return False
            cur = await conn.execute(
                "SELECT id, title, content_text, visibility FROM document"
                " WHERE id = %s",
                (document_id,))
            row = await cur.fetchone()
            if row is None:
                return False
            if row["visibility"] != "shared":
                # I1 gate 1: T1 persistence writes shared-KB rows; fetched
                # docs are always shared, this guards the dedup edge cases
                return False
            model = provider.model_for(ModelTier.FAST)
            projected = spend.estimated_t1_cost(model, batch=False)
            try:
                await self.governor.check(projected)
                budget.check(projected)
            except RuntimeError as e:
                log.warning("investigation T1 skipped (doc %s): %s",
                            document_id, e)
                return False
            try:
                completion = await t1_mod.extract(
                    provider, title=row["title"],
                    content_text=row["content_text"])
            except LLMError as e:
                log.warning("investigation T1 failed (doc %s): %s",
                            document_id, e)
                await t1_persist.mark_failed(conn, document_id)
                return False
            await spend.record_call(conn,
                                    purpose=PURPOSE_INVESTIGATION_T1,
                                    model=completion.model,
                                    usage=completion.usage)
            budget.add(spend.cost_usd(
                completion.model,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                cache_read_tokens=completion.usage.cache_read_tokens))
            try:
                await t1_persist.persist_t1(
                    conn, document_id=document_id,
                    result=completion.output, model=completion.model)
            except psycopg.Error:
                log.exception("investigation T1 persist failed (doc %s)",
                              document_id)
                await t1_persist.mark_failed(conn, document_id)
                return False
            return True

        return _enrich

    async def _save_usage(self, conn: psycopg.AsyncConnection,
                          dossier_id: int, budget: AnalysisBudget,
                          state: InvestigationState | None) -> None:
        usage: dict[str, Any] = {"cost_usd": round(budget.spent_usd, 6)}
        if state is not None:
            usage["docs_added"] = state.docs_added
            usage["web_fetches_used"] = state.web_fetches_used
        else:
            cur = await conn.execute(
                "SELECT model_usage FROM dossier WHERE id = %s",
                (dossier_id,))
            row = await cur.fetchone()
            if row is not None:
                prior = (row["model_usage"]
                         if isinstance(row["model_usage"], dict) else {})
                usage = {**prior, **usage}
        async with conn.transaction():
            await conn.execute(
                "UPDATE dossier SET model_usage = %s WHERE id = %s",
                (Jsonb(usage), dossier_id))

    async def _section_start(self, conn: psycopg.AsyncConnection,
                             dossier_id: int, stage: str) -> None:
        now = utc_now()
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at) VALUES (%s,%s, 'running', '{}', %s)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status='running', content='{}'::jsonb,"
                " created_at=EXCLUDED.created_at, updated_at=NULL",
                (dossier_id, stage, now))
            await conn.execute(
                "UPDATE dossier SET current_stage = %s WHERE id = %s",
                (stage, dossier_id))

    async def _section_complete(self, conn: psycopg.AsyncConnection,
                                dossier_id: int, stage: str,
                                content: dict[str, Any]) -> None:
        await self._write_section(conn, dossier_id, stage, content,
                                  status="completed")

    async def _write_section(self, conn: psycopg.AsyncConnection,
                             dossier_id: int, stage: str,
                             content: dict[str, Any],
                             status: str = "completed") -> None:
        now = utc_now()
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status=EXCLUDED.status, content=EXCLUDED.content,"
                " updated_at=EXCLUDED.updated_at",
                (dossier_id, stage, status, Jsonb(content), now, now))

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
