"""The T1 sweep — selection + sync/batch delivery, budget-governed.

Selection: enrichment_status='pending', not a near-dup, and the source is
not t1_exempt UNLESS the document is a watch hit (watch_hit overrides the
exemption; sourceless manual ingests are always eligible). media_type is
irrelevant — tweets and PDFs enrich the same way.

Sync mode: loop with Governor.check before EVERY call; BudgetExceeded stops
the loop cleanly — the remainder stays 'pending' and is picked up by a later
sweep. Batch mode: ONE Message Batch (50% off), governor checked on the
projected cost BEFORE submitting; docs are marked 'queued' while the job
polls, and results are ingested through the SAME persist path as sync.

The fast path (watch-hit / fact-checker docs straight after ingest) is
``enrich_document`` — a single-doc sync enrichment submitted as an
'enrich_t1_sync' job by the composition root.

v0.2: the service holds the pool only for its Governor; every unit of work
receives the caller's connection (the job's connection, in production).

Phase 2: T2 (event clustering + story threading) is appended to BOTH
delivery paths — after a successful T1 persist the promotion triggers are
evaluated and, when any fires, t2.process_document runs (its LLM calls are
governor-checked individually; its deterministic paths are free). A T2
failure never fails the T1 result.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

import psycopg
import pydantic

from connect.knowledge.enrichment import persist, promotion, t1, t2
from connect.knowledge.enrichment.promotion import (  # noqa: F401 — re-export
    is_fact_checker_source,
)
from connect.knowledge.enrichment.prompts import T1_PROMPT_VERSION
from connect.knowledge.linking import position_tracker
from connect.llm import spend
from connect.llm.batch_runner import BatchItem, BatchRunner, BatchResult
from connect.llm.observability import LLMTracer, null_tracer
from connect.llm.provider import LLMError, LLMProvider, Usage
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier

log = logging.getLogger(__name__)

DEFAULT_SWEEP_LIMIT = 200
PURPOSE_T1 = "enrich_t1"

_Row = Mapping[str, Any]

# Gate 1 of invariant I1 (tenancy design §1): ONLY shared documents are
# enrichment-eligible — private docs simply stay 'pending' and the next
# sweep after a share picks them up; no special status, no special action.
_ELIGIBLE_SQL = """
SELECT d.id, d.title, d.content_text
FROM document d
LEFT JOIN source s ON s.id = d.source_id
WHERE d.enrichment_status = 'pending'
  AND d.visibility = 'shared'
  AND d.canonical_document_id IS NULL
  AND (s.id IS NULL OR NOT s.t1_exempt OR d.watch_hit)
ORDER BY d.id
LIMIT %s
"""

# v9 backfill (target='statements'): docs already T1-enriched under a prompt
# older than t1-v2 (no statement extraction yet). Full re-extraction —
# persist_t1 is an idempotent per-doc replace. The visibility predicate is
# belt-and-braces: enriched docs are shared by I1.
_BACKFILL_STATEMENTS_SQL = """
SELECT d.id, d.title, d.content_text
FROM document d
JOIN document_enrichment de ON de.document_id = d.id
WHERE de.prompt_version < %s
  AND d.visibility = 'shared'
ORDER BY d.id
LIMIT %s
"""


class EnrichmentService:
    def __init__(self, *, provider: LLMProvider | None,
                 batch_runner: BatchRunner | None,
                 governor: Governor,
                 batch_poll_seconds: float = 30.0,
                 tracer: LLMTracer | None = None):
        self.provider = provider
        self.batch_runner = batch_runner
        self.governor = governor
        self.batch_poll_seconds = batch_poll_seconds
        # batch enrichment runs the Message Batches API directly (not through
        # AnthropicProvider), so it carries its own tracer to record one
        # Langfuse generation per item once results land.
        self.tracer = tracer or null_tracer()

    # -- selection ----------------------------------------------------------------

    async def select_eligible(self, conn: psycopg.AsyncConnection,
                              limit: int = DEFAULT_SWEEP_LIMIT, *,
                              target: str | None = None) -> list[_Row]:
        """target=None: pending docs (the normal sweep). target='statements':
        docs already enriched with prompt_version < t1-v2 (the backfill)."""
        if target == "statements":
            cur = await conn.execute(
                _BACKFILL_STATEMENTS_SQL, (T1_PROMPT_VERSION, limit))
            return await cur.fetchall()
        cur = await conn.execute(_ELIGIBLE_SQL, (limit,))
        return await cur.fetchall()

    # -- sync ----------------------------------------------------------------------

    async def enrich_document(self, conn: psycopg.AsyncConnection,
                              document_id: int) -> str:
        """Fast path: enrich ONE document synchronously (governor-checked).
        Returns a short status string (job result)."""
        provider = self._require_provider()
        cur = await conn.execute(
            "SELECT id, title, content_text, enrichment_status, visibility"
            " FROM document WHERE id = %s", (document_id,))
        row = await cur.fetchone()
        if row is None:
            return f"document {document_id} not found"
        if row["visibility"] != "shared":  # I1 gate 1 — same predicate as
            return ("skipped: private document"  # the sweep's _ELIGIBLE_SQL
                    " (stays pending until shared)")
        model = provider.model_for(ModelTier.FAST)
        try:
            await self.governor.check(
                spend.estimated_t1_cost(model, batch=False))
        except BudgetExceeded as e:
            log.warning("fast-path enrich skipped (doc %s): %s",
                        document_id, e)
            return f"skipped: {e}"
        stats = await self._enrich_one(conn, provider, row)
        return f"done: {stats}" if stats is not None else "failed"

    async def promote_document(self, conn: psycopg.AsyncConnection,
                               document_id: int) -> str:
        """Manual T2 promotion (POST /api/documents/{id}/promote, run as an
        'enrich_t2' job). Ensures T1 first (governor-checked), then runs T2
        with the 'manual' trigger. Returns a short status string."""
        cur = await conn.execute(
            "SELECT id, visibility FROM document WHERE id = %s",
            (document_id,))
        row = await cur.fetchone()
        if row is None:
            return f"document {document_id} not found"
        if row["visibility"] != "shared":
            # I1 gate 1: T2 writes events/edges/assignments — shared tables
            return "skipped: private document cannot enter the shared KB"
        cur = await conn.execute(
            "SELECT 1 FROM document_enrichment WHERE document_id = %s",
            (document_id,))
        has_t1 = await cur.fetchone() is not None
        if not has_t1:
            if self.provider is None:
                return "skipped: ANTHROPIC_API_KEY not set (T1 required)"
            t1_status = await self.enrich_document(conn, document_id)
            if not t1_status.startswith("done"):
                return f"t1 {t1_status}"
        stats = await self._maybe_t2(conn, document_id, manual=True)
        return f"promoted: {stats}"

    async def run_sync(self, conn: psycopg.AsyncConnection,
                       limit: int | None = None, *,
                       target: str | None = None) -> dict[str, Any]:
        """Inline sweep: one FAST call per eligible doc, stopping cleanly on
        BudgetExceeded (remainder stays 'pending')."""
        provider = self._require_provider()
        rows = await self.select_eligible(
            conn, limit or DEFAULT_SWEEP_LIMIT, target=target)
        model = provider.model_for(ModelTier.FAST)
        per_call = spend.estimated_t1_cost(model, batch=False)
        done = failed = 0
        halted = False
        for row in rows:
            try:
                await self.governor.check(per_call)
            except BudgetExceeded as e:
                log.warning("sync sweep halted by governor: %s", e)
                halted = True
                break
            stats = await self._enrich_one(conn, provider, row)
            if stats is None:
                failed += 1
            else:
                done += 1
        remaining = len(rows) - done - failed
        return {"mode": "sync", "selected": len(rows), "done": done,
                "failed": failed, "remaining_pending": remaining,
                "halted_budget": halted}

    async def _enrich_one(self, conn: psycopg.AsyncConnection,
                          provider: LLMProvider,
                          row: _Row) -> dict[str, Any] | None:
        """Extract + ledger + persist one document; None on failure.
        Promoted docs get T2 appended (never fatal to the T1 result)."""
        try:
            completion = await t1.extract(
                provider, title=row["title"], content_text=row["content_text"])
        except LLMError as e:
            log.warning("T1 extraction failed for document %s: %s",
                        row["id"], e)
            await persist.mark_failed(conn, row["id"])
            return None
        await spend.record_call(conn, purpose=PURPOSE_T1,
                                model=completion.model,
                                usage=completion.usage)
        try:
            stats = await persist.persist_t1(
                conn, document_id=row["id"], result=completion.output,
                model=completion.model)
        except psycopg.Error:
            log.exception("T1 persistence failed for document %s", row["id"])
            await persist.mark_failed(conn, row["id"])
            return None
        shift_stats = await self._maybe_shifts(conn, row["id"])
        if shift_stats is not None:
            stats["shifts"] = shift_stats
        t2_stats = await self._maybe_t2(conn, row["id"])
        if t2_stats is not None:
            stats["t2"] = t2_stats
        return stats

    async def _maybe_shifts(self, conn: psycopg.AsyncConnection,
                            document_id: int) -> dict[str, Any] | None:
        """Position-shift detection over the doc's freshly persisted
        statements (knowledge/linking/position_tracker). Never raises —
        a tracker failure is logged and the T1 result stands."""
        try:
            return await position_tracker.detect_shifts(
                conn, self.provider, self.governor, document_id)
        except Exception:  # noqa: BLE001 — tracking must never fail T1
            log.exception("shift detection failed for document %s",
                          document_id)
            return {"document_id": document_id, "error": "shifts_failed"}

    async def _maybe_t2(self, conn: psycopg.AsyncConnection,
                        document_id: int, *,
                        manual: bool = False) -> dict[str, Any] | None:
        """Evaluate promotion triggers; run T2 when any fires. Never raises
        — a T2 failure is logged and the T1 result stands."""
        try:
            triggers = await promotion.evaluate(conn, document_id,
                                                manual=manual)
            if not triggers:
                return None
            return await t2.process_document(
                conn, self.provider, self.governor, document_id,
                triggers=triggers)
        except Exception:  # noqa: BLE001 — T2 must never fail T1
            log.exception("T2 processing failed for document %s",
                          document_id)
            return {"document_id": document_id, "error": "t2_failed"}

    # -- batch ----------------------------------------------------------------------

    async def run_batch(self, conn: psycopg.AsyncConnection,
                        limit: int | None = None, *,
                        target: str | None = None) -> dict[str, Any]:
        """One Message Batch over the eligible set; polls until ended and
        ingests results through the same persist path as sync."""
        provider = self._require_provider()
        runner = self.batch_runner
        if runner is None:
            raise LLMError("batch runner not configured "
                           "(ANTHROPIC_API_KEY not set)")
        rows = await self.select_eligible(
            conn, limit or DEFAULT_SWEEP_LIMIT, target=target)
        if not rows:
            return {"mode": "batch", "selected": 0, "submitted": 0}

        model = provider.model_for(ModelTier.FAST)
        projected = len(rows) * spend.estimated_t1_cost(model, batch=True)
        try:
            await self.governor.check(projected)
        except BudgetExceeded as e:
            log.warning("batch sweep refused by governor: %s", e)
            return {"mode": "batch", "selected": len(rows), "submitted": 0,
                    "halted_budget": True, "reason": str(e)}

        items = [
            BatchItem(
                custom_id=f"doc-{row['id']}",
                model=model,
                system=t1.T1_SYSTEM,
                user_text=t1.build_user_message(
                    row["title"], row["content_text"]),
                max_tokens=t1.T1_MAX_OUTPUT_TOKENS,
            )
            for row in rows
        ]
        doc_ids = [row["id"] for row in rows]
        batch_id = await runner.submit(items, schema=t1.EnrichmentT1)
        await persist.mark_queued(conn, doc_ids)
        log.info("submitted T1 batch %s (%s docs)", batch_id, len(doc_ids))

        while await runner.status(batch_id) != "ended":
            await asyncio.sleep(self.batch_poll_seconds)

        results = await runner.results(batch_id)
        items_by_id = {item.custom_id: item for item in items}
        stats = await self._ingest_batch_results(conn, results, model=model,
                                                 batch_id=batch_id,
                                                 items=items_by_id)
        # anything still 'queued' got no result row back — release it
        cur = await conn.execute(
            "SELECT id FROM document WHERE enrichment_status = 'queued'"
            " AND id = ANY(%s)", (doc_ids,))
        leftover = [r["id"] for r in await cur.fetchall()]
        if leftover:
            await persist.mark_pending(conn, leftover)
        return {"mode": "batch", "selected": len(rows),
                "submitted": len(items), "batch_id": batch_id,
                "released_pending": len(leftover), **stats}

    async def _ingest_batch_results(self, conn: psycopg.AsyncConnection,
                                    results: list[BatchResult], *,
                                    model: str,
                                    batch_id: str,
                                    items: Mapping[str, BatchItem]
                                    | None = None,
                                    ) -> dict[str, int]:
        items = items or {}
        done = failed = promoted = 0
        for result in results:
            try:
                document_id = int(result.custom_id.split("-", 1)[1])
            except (IndexError, ValueError):
                log.warning("unrecognized batch custom_id %r",
                            result.custom_id)
                continue
            self._trace_batch_item(result, items.get(result.custom_id),
                                   model=model, batch_id=batch_id,
                                   document_id=document_id)
            if not result.ok:
                log.warning("batch item failed for document %s: %s",
                            document_id, result.error)
                await persist.mark_failed(conn, document_id)
                failed += 1
                continue
            await spend.record_call(conn, purpose=PURPOSE_T1, model=model,
                                    usage=result.usage, batch=True,
                                    batch_id=batch_id)
            try:
                parsed = t1.EnrichmentT1.model_validate_json(result.text)
            except (pydantic.ValidationError, ValueError) as e:
                log.warning("batch item unparseable for document %s: %s",
                            document_id, e)
                await persist.mark_failed(conn, document_id)
                failed += 1
                continue
            await persist.persist_t1(conn, document_id=document_id,
                                     result=parsed, model=model,
                                     prompt_version=T1_PROMPT_VERSION)
            done += 1
            await self._maybe_shifts(conn, document_id)
            if await self._maybe_t2(conn, document_id) is not None:
                promoted += 1
        return {"done": done, "failed": failed, "promoted_t2": promoted}

    def _trace_batch_item(self, result: BatchResult,
                          item: BatchItem | None, *, model: str,
                          batch_id: str, document_id: int) -> None:
        """Record one batch result as a Langfuse generation (no-op when
        tracing is off). The Message Batches path bypasses AnthropicProvider,
        so this is its only instrumentation — input comes from the submitted
        item, output/usage from the result."""
        if not self.tracer.enabled:
            return
        input_payload: Any = {"custom_id": result.custom_id}
        if item is not None:
            input_payload = {
                "system": item.system,
                "messages": [{"role": "user", "content": item.user_text}]}
        self.tracer.record_generation(
            name="enrich_t1_batch", model=model,
            input=input_payload,
            output=result.text if result.ok else None,
            usage=result.usage if result.ok else Usage(),
            tier=ModelTier.FAST,
            extra={"purpose": PURPOSE_T1, "batch_id": batch_id,
                   "document_id": document_id, "batch": True},
            is_error=not result.ok,
            status_message=None if result.ok else result.error)

    # -- internals --------------------------------------------------------------------

    def _require_provider(self) -> LLMProvider:
        if self.provider is None:
            raise LLMError("LLM provider not configured "
                           "(ANTHROPIC_API_KEY not set)")
        return self.provider
