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

Phase 2: T2 (event clustering + story threading) is appended to BOTH
delivery paths — after a successful T1 persist the promotion triggers are
evaluated and, when any fires, t2.process_document runs (its LLM calls are
governor-checked individually; its deterministic paths are free). A T2
failure never fails the T1 result.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

import pydantic

from connect.knowledge.enrichment import persist, promotion, t1, t2
from connect.knowledge.enrichment.promotion import (  # noqa: F401 — re-export
    is_fact_checker_source,
)
from connect.knowledge.enrichment.prompts import T1_PROMPT_VERSION
from connect.llm import spend
from connect.llm.batch_runner import BatchItem, BatchRunner, BatchResult
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier

log = logging.getLogger(__name__)

DEFAULT_SWEEP_LIMIT = 200
PURPOSE_T1 = "enrich_t1"

_ELIGIBLE_SQL = """
SELECT d.id, d.title, d.content_text
FROM document d
LEFT JOIN source s ON s.id = d.source_id
WHERE d.enrichment_status = 'pending'
  AND d.canonical_document_id IS NULL
  AND (s.id IS NULL OR s.t1_exempt = 0 OR d.watch_hit = 1)
ORDER BY d.id
LIMIT ?
"""


class EnrichmentService:
    def __init__(self, conn: sqlite3.Connection, *,
                 provider: LLMProvider | None,
                 batch_runner: BatchRunner | None,
                 governor: Governor,
                 batch_poll_seconds: float = 30.0):
        self.conn = conn
        self.provider = provider
        self.batch_runner = batch_runner
        self.governor = governor
        self.batch_poll_seconds = batch_poll_seconds

    # -- selection ----------------------------------------------------------------

    def select_eligible(self, limit: int = DEFAULT_SWEEP_LIMIT,
                        ) -> list[sqlite3.Row]:
        return self.conn.execute(_ELIGIBLE_SQL, (limit,)).fetchall()

    # -- sync ----------------------------------------------------------------------

    async def enrich_document(self, document_id: int) -> str:
        """Fast path: enrich ONE document synchronously (governor-checked).
        Returns a short status string (job result)."""
        provider = self._require_provider()
        row = self.conn.execute(
            "SELECT id, title, content_text, enrichment_status"
            " FROM document WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return f"document {document_id} not found"
        model = provider.model_for(ModelTier.FAST)
        try:
            self.governor.check(spend.estimated_t1_cost(model, batch=False))
        except BudgetExceeded as e:
            log.warning("fast-path enrich skipped (doc %s): %s",
                        document_id, e)
            return f"skipped: {e}"
        stats = await self._enrich_one(provider, row)
        return f"done: {stats}" if stats is not None else "failed"

    async def promote_document(self, document_id: int) -> str:
        """Manual T2 promotion (POST /api/documents/{id}/promote, run as an
        'enrich_t2' job). Ensures T1 first (governor-checked), then runs T2
        with the 'manual' trigger. Returns a short status string."""
        row = self.conn.execute(
            "SELECT id FROM document WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return f"document {document_id} not found"
        has_t1 = self.conn.execute(
            "SELECT 1 FROM document_enrichment WHERE document_id = ?",
            (document_id,)).fetchone() is not None
        if not has_t1:
            if self.provider is None:
                return "skipped: ANTHROPIC_API_KEY not set (T1 required)"
            t1_status = await self.enrich_document(document_id)
            if not t1_status.startswith("done"):
                return f"t1 {t1_status}"
        stats = await self._maybe_t2(document_id, manual=True)
        return f"promoted: {stats}"

    async def run_sync(self, limit: int | None = None) -> dict[str, Any]:
        """Inline sweep: one FAST call per eligible doc, stopping cleanly on
        BudgetExceeded (remainder stays 'pending')."""
        provider = self._require_provider()
        rows = self.select_eligible(limit or DEFAULT_SWEEP_LIMIT)
        model = provider.model_for(ModelTier.FAST)
        per_call = spend.estimated_t1_cost(model, batch=False)
        done = failed = 0
        halted = False
        for row in rows:
            try:
                self.governor.check(per_call)
            except BudgetExceeded as e:
                log.warning("sync sweep halted by governor: %s", e)
                halted = True
                break
            stats = await self._enrich_one(provider, row)
            if stats is None:
                failed += 1
            else:
                done += 1
        remaining = len(rows) - done - failed
        return {"mode": "sync", "selected": len(rows), "done": done,
                "failed": failed, "remaining_pending": remaining,
                "halted_budget": halted}

    async def _enrich_one(self, provider: LLMProvider,
                          row: sqlite3.Row) -> dict[str, Any] | None:
        """Extract + ledger + persist one document; None on failure.
        Promoted docs get T2 appended (never fatal to the T1 result)."""
        try:
            completion = await t1.extract(
                provider, title=row["title"], content_text=row["content_text"])
        except LLMError as e:
            log.warning("T1 extraction failed for document %s: %s",
                        row["id"], e)
            persist.mark_failed(self.conn, row["id"])
            return None
        spend.record_call(self.conn, purpose=PURPOSE_T1,
                          model=completion.model, usage=completion.usage)
        try:
            stats = persist.persist_t1(
                self.conn, document_id=row["id"], result=completion.output,
                model=completion.model)
        except sqlite3.Error:
            log.exception("T1 persistence failed for document %s", row["id"])
            persist.mark_failed(self.conn, row["id"])
            return None
        t2_stats = await self._maybe_t2(row["id"])
        if t2_stats is not None:
            stats["t2"] = t2_stats
        return stats

    async def _maybe_t2(self, document_id: int, *,
                        manual: bool = False) -> dict[str, Any] | None:
        """Evaluate promotion triggers; run T2 when any fires. Never raises
        — a T2 failure is logged and the T1 result stands."""
        try:
            triggers = promotion.evaluate(self.conn, document_id,
                                          manual=manual)
            if not triggers:
                return None
            return await t2.process_document(
                self.conn, self.provider, self.governor, document_id,
                triggers=triggers)
        except Exception:  # noqa: BLE001 — T2 must never fail T1
            log.exception("T2 processing failed for document %s",
                          document_id)
            return {"document_id": document_id, "error": "t2_failed"}

    # -- batch ----------------------------------------------------------------------

    async def run_batch(self, limit: int | None = None) -> dict[str, Any]:
        """One Message Batch over the eligible set; polls until ended and
        ingests results through the same persist path as sync."""
        provider = self._require_provider()
        runner = self.batch_runner
        if runner is None:
            raise LLMError("batch runner not configured "
                           "(ANTHROPIC_API_KEY not set)")
        rows = self.select_eligible(limit or DEFAULT_SWEEP_LIMIT)
        if not rows:
            return {"mode": "batch", "selected": 0, "submitted": 0}

        model = provider.model_for(ModelTier.FAST)
        projected = len(rows) * spend.estimated_t1_cost(model, batch=True)
        try:
            self.governor.check(projected)
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
        persist.mark_queued(self.conn, doc_ids)
        log.info("submitted T1 batch %s (%s docs)", batch_id, len(doc_ids))

        while await runner.status(batch_id) != "ended":
            await asyncio.sleep(self.batch_poll_seconds)

        results = await runner.results(batch_id)
        stats = await self._ingest_batch_results(results, model=model,
                                                 batch_id=batch_id)
        # anything still 'queued' got no result row back — release it
        leftover = [
            r[0] for r in self.conn.execute(
                "SELECT id FROM document WHERE enrichment_status = 'queued'"
                " AND id IN (%s)" % ",".join("?" * len(doc_ids)), doc_ids)]
        if leftover:
            persist.mark_pending(self.conn, leftover)
        return {"mode": "batch", "selected": len(rows),
                "submitted": len(items), "batch_id": batch_id,
                "released_pending": len(leftover), **stats}

    async def _ingest_batch_results(self, results: list[BatchResult], *,
                                    model: str,
                                    batch_id: str) -> dict[str, int]:
        done = failed = promoted = 0
        for result in results:
            try:
                document_id = int(result.custom_id.split("-", 1)[1])
            except (IndexError, ValueError):
                log.warning("unrecognized batch custom_id %r",
                            result.custom_id)
                continue
            if not result.ok:
                log.warning("batch item failed for document %s: %s",
                            document_id, result.error)
                persist.mark_failed(self.conn, document_id)
                failed += 1
                continue
            spend.record_call(self.conn, purpose=PURPOSE_T1, model=model,
                              usage=result.usage, batch=True,
                              batch_id=batch_id)
            try:
                parsed = t1.EnrichmentT1.model_validate_json(result.text)
            except (pydantic.ValidationError, ValueError) as e:
                log.warning("batch item unparseable for document %s: %s",
                            document_id, e)
                persist.mark_failed(self.conn, document_id)
                failed += 1
                continue
            persist.persist_t1(self.conn, document_id=document_id,
                               result=parsed, model=model,
                               prompt_version=T1_PROMPT_VERSION)
            done += 1
            if await self._maybe_t2(document_id) is not None:
                promoted += 1
        return {"done": done, "failed": failed, "promoted_t2": promoted}

    # -- internals --------------------------------------------------------------------

    def _require_provider(self) -> LLMProvider:
        if self.provider is None:
            raise LLMError("LLM provider not configured "
                           "(ANTHROPIC_API_KEY not set)")
        return self.provider
