"""Editorial-integrity job bodies (S3): corrections propagation + source
re-checking. Importing this module registers the handlers."""

from __future__ import annotations

import logging
from typing import Any

from connect.integrity import evaluate as integrity_eval
from connect.knowledge import corrections, credibility
from connect.workers.registry import WorkerContext, register

log = logging.getLogger(__name__)


@register("content_correction")
async def run_content_correction(ctx: WorkerContext,
                                 payload: dict[str, Any]) -> Any:
    """Sweep open corrections and fan them out to dependent content_items
    (verdict flips + source edits/retractions). Idempotent."""
    return await corrections.propagate_open(ctx.conn)


@register("source_recheck")
async def run_source_recheck(ctx: WorkerContext,
                             payload: dict[str, Any]) -> Any:
    """Re-fetch one document's URL and, if its figures/body changed, open a
    source_edit correction. Conservative: a transient fetch error is skipped
    (never a false retraction). The next content_correction sweep propagates."""
    document_id = int(payload["document_id"])
    cur = await ctx.conn.execute(
        "SELECT url FROM document WHERE id = %s", (document_id,))
    row = await cur.fetchone()
    if row is None or not row["url"]:
        return "skip"
    fetcher = getattr(ctx.services, "fetcher", None)
    if fetcher is None:
        return "no_fetcher"
    try:
        from connect.ingestion.extract_html import extract_html
        res = await fetcher.fetch(row["url"])
        new_text = extract_html(res.content, row["url"]).text
    except Exception as e:  # noqa: BLE001 — transient/blocked: skip, don't retract
        log.info("source_recheck %s: fetch/extract failed: %s", document_id, e)
        return "fetch_failed"
    return await corrections.recheck_document(
        ctx.conn, document_id, gone=False, new_text=new_text) or "unchanged"


@register("credibility_recompute")
async def run_credibility_recompute(ctx: WorkerContext,
                                    payload: dict[str, Any]) -> Any:
    """S4: recompute dynamic source-reliability from track record. Optional
    payload `source_ids` scopes it; default recomputes all enabled sources."""
    source_ids = payload.get("source_ids")
    scores = await credibility.recompute(
        ctx.conn, source_ids=[int(s) for s in source_ids] if source_ids
        else None, trigger=str(payload.get("trigger") or "nightly"))
    return f"{len(scores)} sources"


@register("integrity_eval")
async def run_integrity_eval(ctx: WorkerContext,
                             payload: dict[str, Any]) -> Any:
    """S5: snapshot live integrity metrics, check floors + rolling baseline,
    persist an integrity_eval_run, alert on regression. No LLM/gold needed."""
    run = await integrity_eval.run_eval(
        ctx.conn, days=int(payload.get("days") or 1),
        trigger=str(payload.get("trigger") or "nightly"))
    return run["status"]
