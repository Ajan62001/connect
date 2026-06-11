"""InvestigationService — dossier lifecycle + the MANUAL tool loop
(design §2.3). One 'investigation' job per run over the existing JobRunner;
job_event.seq is the SSE id, exactly the Phase-3 analyses contract.

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
import json
import logging
import sqlite3
from typing import Any

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
from connect.llm.provider import (
    LLMError,
    LLMProvider,
    assistant_message,
    tool_result_message,
)
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.orchestration import events
from connect.orchestration.jobs import JobRunner
from connect.retrieval.search_client import SearchClient
from connect.sources.seeds import WEB_INVESTIGATION_SOURCE
from connect.storage.db import utc_now

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
    def __init__(self, conn: sqlite3.Connection, *, jobs: JobRunner,
                 provider: LLMProvider | None, governor: Governor,
                 search: SearchClient, ingest: IngestionPipeline | None,
                 embedder: Embedder, vectors: VectorIndex | None,
                 default_budget_usd: float = 1.00,
                 default_max_iterations: int = 14,
                 default_max_web_fetches: int = 8,
                 synthesis_reserve_usd: float = 0.20,
                 synthesis_tier: str = "deep"):
        self.conn = conn
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

    def start(self, seed: InvestigationSeed,
              options: InvestigationOptions | None = None,
              ) -> tuple[int, int]:
        """Create dossier(kind='investigation') + the job; returns
        (investigation_id, job_id). Raises LookupError when the seed
        references a missing row."""
        opts = options or self.default_options()
        input_text, input_type, parent_question_id = scoping.resolve_seed(
            self.conn, seed)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO dossier (kind, input_text, input_type,"
                " parent_question_id, budget_usd, status, created_at)"
                " VALUES ('investigation', ?, ?, ?, ?, 'pending', ?)",
                (input_text, input_type, parent_question_id,
                 opts.budget_usd, utc_now()))
            dossier_id = int(cur.lastrowid)  # type: ignore[arg-type]
            if parent_question_id is not None:
                self.conn.execute(
                    "UPDATE question SET spawned_dossier_id = ?,"
                    " updated_at = ? WHERE id = ?",
                    (dossier_id, utc_now(), parent_question_id))

        holder: dict[str, int] = {}

        async def _run():
            return await self._run_investigation(
                dossier_id, seed, opts, holder["job_id"])

        job_id = self.jobs.submit(
            "investigation",
            {"dossier_id": dossier_id, "options": opts.model_dump()}, _run)
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
        row = self.conn.execute(
            "SELECT status FROM dossier WHERE id = ?"
            " AND kind = 'investigation'", (dossier_id,)).fetchone()
        if row is None or row["status"] not in ("pending", "running"):
            return False
        job_id = self.job_for(dossier_id)
        if job_id is not None:
            self.jobs.cancel_job(job_id)
        self._set_dossier(dossier_id, "cancelled", finished=True)
        return True

    # -- the job coroutine -----------------------------------------------------------

    async def _run_investigation(self, dossier_id: int,
                                 seed: InvestigationSeed,
                                 opts: InvestigationOptions,
                                 job_id: int) -> None:
        if self.provider is None:
            self._set_dossier(dossier_id, "failed", finished=True,
                              error="ANTHROPIC_API_KEY not set")
            raise LLMError("LLM provider not configured "
                           "(ANTHROPIC_API_KEY not set)")

        def emit(type_: str, data: dict[str, Any] | None = None) -> int:
            return events.emit(self.conn, job_id, type_, data)

        self._set_dossier(dossier_id, "running", started=True)
        budget = AnalysisBudget(opts.budget_usd)
        stage = "scope"
        try:
            pack = self._stage_scope(dossier_id, seed, emit)
            stage = "investigate"
            state = await self._stage_investigate(
                dossier_id, pack, opts, budget, emit)
            stage = "synthesize"
            await self._stage_synthesize(
                dossier_id, pack, budget, state, emit)
        except asyncio.CancelledError:
            self._section_finish(dossier_id, stage, "failed")
            self._set_dossier(dossier_id, "cancelled", finished=True)
            self._save_usage(dossier_id, budget, None)
            raise
        except Exception as e:
            self._section_finish(dossier_id, stage, "failed")
            self._set_dossier(dossier_id, "failed", finished=True,
                              error=str(e))
            self._save_usage(dossier_id, budget, None)
            raise
        self._set_dossier(dossier_id, "completed", finished=True)
        return None  # JobRunner's 'done' event carries empty data

    # -- stage: scope (zero LLM) -------------------------------------------------------

    def _stage_scope(self, dossier_id: int, seed: InvestigationSeed,
                     emit) -> ScopePack:
        self._section_start(dossier_id, "scope")
        row = self.conn.execute(
            "SELECT input_text, input_type FROM dossier WHERE id = ?",
            (dossier_id,)).fetchone()
        pack = scoping.build_scope_pack(
            self.conn, embedder=self.embedder, vectors=self.vectors,
            input_text=row["input_text"], input_type=row["input_type"],
            seed=seed)
        summary = (f"{len(pack.documents)} docs, {len(pack.anchors)}"
                   f" anchors, {len(pack.timeline)} timeline items,"
                   f" {len(pack.reaction_candidates)} reaction candidates,"
                   f" coverage {pack.coverage}")
        self._section_complete(dossier_id, "scope",
                               {"summary": summary, **pack.model_dump()})
        emit("section_completed", {"section": "scope", "summary": summary})
        return pack

    # -- stage: investigate (the manual loop) --------------------------------------------

    async def _stage_investigate(self, dossier_id: int, pack: ScopePack,
                                 opts: InvestigationOptions,
                                 budget: AnalysisBudget,
                                 emit) -> InvestigationState:
        assert self.provider is not None
        self._section_start(dossier_id, "investigate")
        await questions_mod.generate_questions(
            self.conn, self.provider, dossier_id=dossier_id, pack=pack,
            governor=self.governor, budget=budget, emit=emit)

        state = InvestigationState(
            dossier_id=dossier_id, max_web_fetches=opts.max_web_fetches)
        executor = ToolExecutor(
            self.conn, pipeline=self.ingest, search=self.search,
            embedder=self.embedder, vectors=self.vectors, scope_pack=pack,
            state=state, emit=emit,
            web_source_id=self._web_source_id(),
            t1_enrich=self._make_t1_enricher(budget))

        model = self.provider.model_for(ModelTier.BALANCED)
        turn_proj = spend.cost_usd(model, input_tokens=EST_LOOP_IN,
                                   output_tokens=EST_LOOP_OUT)
        loop_cap = max(0.0, opts.budget_usd - self.synthesis_reserve_usd)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": scoping.render_scope_pack(pack)
            + questions_mod.render_questions(self.conn, dossier_id)}]

        iterations = 0
        force_next = False
        for n in range(1, opts.max_iterations + 1):
            if state.concluded:
                break
            spent = budget.spent_usd
            if (not state.corpus_only
                    and spent >= CORPUS_ONLY_FRACTION * opts.budget_usd):
                state.corpus_only = True
            force = (force_next or n == opts.max_iterations
                     or spent >= FORCE_CONCLUDE_FRACTION * opts.budget_usd
                     or spent + turn_proj > loop_cap)
            if not force:
                try:
                    self.governor.check(turn_proj)
                except BudgetExceeded:
                    force = True  # one last forced-conclude call
            turn = await self.provider.complete_with_tools(
                system=INVESTIGATOR_SYSTEM, messages=messages,
                tools=TOOL_DEFS, tier=ModelTier.BALANCED,
                max_tokens=LOOP_MAX_TOKENS,
                tool_choice="conclude" if force else None)
            spend.record_call(self.conn, purpose=PURPOSE_INVESTIGATION,
                              model=turn.model, usage=turn.usage)
            budget.add(spend.cost_usd(
                turn.model, input_tokens=turn.usage.input_tokens,
                output_tokens=turn.usage.output_tokens,
                cache_read_tokens=turn.usage.cache_read_tokens))
            iterations = n
            emit("iteration", {
                "n": n, "tools": [c.name for c in turn.tool_calls],
                "cost_so_far": round(budget.spent_usd, 4),
                "forced": force, "corpus_only": state.corpus_only})
            self._save_usage(dossier_id, budget, state)

            if not turn.tool_calls:
                # end_turn without tools -> forced conclude next turn
                messages.append(assistant_message(turn))
                messages.append({
                    "role": "user",
                    "content": "You must finish now: call the conclude"
                               " tool with honest resolutions for every"
                               " question."})
                force_next = True
                continue
            messages.append(assistant_message(turn))
            results = []
            for call in turn.tool_calls:
                outcome = await executor(call)
                results.append((call, outcome))
            messages.append(tool_result_message(results))

        summary = (f"{iterations} iterations,"
                   f" {state.findings_recorded} findings,"
                   f" {state.questions_raised} questions raised,"
                   f" {state.docs_added} docs added,"
                   f" ${budget.spent_usd:.4f} spent"
                   + ("" if state.concluded else "; loop ended without"
                                                 " conclude"))
        self._section_complete(dossier_id, "investigate", {
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
        emit("section_completed", {"section": "investigate",
                                   "summary": summary})
        return state

    # -- stage: synthesize ---------------------------------------------------------------

    async def _stage_synthesize(self, dossier_id: int, pack: ScopePack,
                                budget: AnalysisBudget,
                                state: InvestigationState, emit) -> None:
        assert self.provider is not None
        self._section_start(dossier_id, "synthesize")
        summary = await synthesize.run(
            self.conn, self.provider, dossier_id=dossier_id, pack=pack,
            budget=budget, governor=self.governor, emit=emit,
            tier_pref=self.synthesis_tier,
            reserve_usd=self.synthesis_reserve_usd,
            section_writer=self._write_section)
        self._section_complete(dossier_id, "synthesize",
                               {"summary": summary})
        emit("section_completed", {"section": "synthesize",
                                   "summary": summary})
        self._save_usage(dossier_id, budget, state)

    # -- plumbing -----------------------------------------------------------------------

    def _web_source_id(self) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM source WHERE name = ?",
            (WEB_INVESTIGATION_SOURCE,)).fetchone()
        return int(row[0]) if row else None

    def _make_t1_enricher(self, budget: AnalysisBudget):
        """Synchronous T1 on investigation-fetched docs, ledgered under
        'investigation_t1' and debited against the per-run budget. Never
        raises into the fetch tool."""
        provider = self.provider

        async def _enrich(document_id: int) -> bool:
            if provider is None:
                return False
            row = self.conn.execute(
                "SELECT id, title, content_text FROM document WHERE id = ?",
                (document_id,)).fetchone()
            if row is None:
                return False
            model = provider.model_for(ModelTier.FAST)
            projected = spend.estimated_t1_cost(model, batch=False)
            try:
                self.governor.check(projected)
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
                t1_persist.mark_failed(self.conn, document_id)
                return False
            spend.record_call(self.conn, purpose=PURPOSE_INVESTIGATION_T1,
                              model=completion.model,
                              usage=completion.usage)
            budget.add(spend.cost_usd(
                completion.model,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                cache_read_tokens=completion.usage.cache_read_tokens))
            try:
                t1_persist.persist_t1(
                    self.conn, document_id=document_id,
                    result=completion.output, model=completion.model)
            except sqlite3.Error:
                log.exception("investigation T1 persist failed (doc %s)",
                              document_id)
                t1_persist.mark_failed(self.conn, document_id)
                return False
            return True

        return _enrich

    def _save_usage(self, dossier_id: int, budget: AnalysisBudget,
                    state: InvestigationState | None) -> None:
        usage: dict[str, Any] = {"cost_usd": round(budget.spent_usd, 6)}
        if state is not None:
            usage["docs_added"] = state.docs_added
            usage["web_fetches_used"] = state.web_fetches_used
        else:
            row = self.conn.execute(
                "SELECT model_usage FROM dossier WHERE id = ?",
                (dossier_id,)).fetchone()
            if row is not None:
                try:
                    prior = json.loads(row["model_usage"] or "{}")
                except ValueError:
                    prior = {}
                usage = {**prior, **usage}
        with self.conn:
            self.conn.execute(
                "UPDATE dossier SET model_usage = ? WHERE id = ?",
                (json.dumps(usage), dossier_id))

    def _section_start(self, dossier_id: int, stage: str) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at) VALUES (?,?, 'running', '{}', ?)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status='running', content='{}', created_at=excluded"
                ".created_at, updated_at=NULL",
                (dossier_id, stage, now))
            self.conn.execute(
                "UPDATE dossier SET current_stage = ? WHERE id = ?",
                (stage, dossier_id))

    def _section_complete(self, dossier_id: int, stage: str,
                          content: dict[str, Any]) -> None:
        self._write_section(dossier_id, stage, content, status="completed")

    def _write_section(self, dossier_id: int, stage: str,
                       content: dict[str, Any],
                       status: str = "completed") -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status=excluded.status, content=excluded.content,"
                " updated_at=excluded.updated_at",
                (dossier_id, stage, status, json.dumps(content), now, now))

    def _section_finish(self, dossier_id: int, stage: str,
                        status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE dossier_section SET status = ?, updated_at = ?"
                " WHERE dossier_id = ? AND stage = ?"
                " AND status = 'running'",
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
