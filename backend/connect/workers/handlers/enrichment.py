"""Enrichment jobs: T1 sync (fast path + manual sweep), T1 batch, and the
manual T2 promotion. All return the short status strings the v0.1 closures
returned (they land in the terminal job_event's ``result``)."""

from __future__ import annotations

from typing import Any

from connect.workers.registry import WorkerContext, register


@register("enrich_t1_sync")
async def enrich_t1_sync(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    """Two payload shapes share the kind (as in v0.1): {document_id} is the
    watch-hit/fact-checker fast path (one doc, straight after ingest);
    {limit, target} is the manual sync sweep."""
    service = ctx.services.enrichment
    assert service is not None, "EnrichmentService not wired"
    if "document_id" in payload:
        return await service.enrich_document(ctx.conn,
                                             payload["document_id"])
    return str(await service.run_sync(ctx.conn, payload.get("limit"),
                                      target=payload.get("target")))


@register("enrich_t1_batch")
async def enrich_t1_batch(ctx: WorkerContext,
                          payload: dict[str, Any]) -> str:
    service = ctx.services.enrichment
    assert service is not None, "EnrichmentService not wired"
    return str(await service.run_batch(ctx.conn, payload.get("limit"),
                                       target=payload.get("target")))


@register("enrich_t2")
async def enrich_t2(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    service = ctx.services.enrichment
    assert service is not None, "EnrichmentService not wired"
    return await service.promote_document(ctx.conn,
                                          payload["document_id"])
