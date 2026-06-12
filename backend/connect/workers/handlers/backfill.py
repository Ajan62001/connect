"""'backfill_source' — historical backfill for one source. The payload
carries the source_id plus the BackfillRequest fields; the handler rebuilds
the Source row, runs the discovery+ingest orchestrator, and records the same
``ok: N new, …`` status shape the poller uses (also written onto the source
row for visibility in the UI).

Re-runnable: discovery + ingest are idempotent (URL-dedup + content-hash),
so a requeued/duplicate backfill never double-ingests."""

from __future__ import annotations

from typing import Any

from connect.sources import backfill as backfill_mod
from connect.sources.attribution import build_domain_index, make_resolver
from connect.storage import sources as source_dao
from connect.workers.registry import WorkerContext, register


@register("backfill_source")
async def backfill_source(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    services = ctx.services
    assert services.pipeline is not None, "IngestionPipeline not wired"
    source = await source_dao.get(ctx.conn, payload["source_id"])
    if source is None:
        raise LookupError(f"source {payload['source_id']} not found")

    # attach discovered articles to the registered source that owns each
    # domain (real credibility tier) when known, else this source.
    resolver = make_resolver(await build_domain_index(ctx.conn))

    result = await backfill_mod.run_backfill(
        ctx.conn, source=source, pipeline=services.pipeline,
        fetcher=services.fetcher,
        methods=payload.get("methods"),
        start=payload.get("start_date"), end=payload.get("end_date"),
        limit=int(payload.get("limit", backfill_mod.DEFAULT_LIMIT)),
        max_pages=int(payload.get("max_pages", 10)),
        page_template=payload.get("page_template"),
        sitemap_urls=payload.get("sitemap_urls") or [],
        manual_urls=payload.get("manual_urls") or [],
        manual_url_template=payload.get("manual_url_template"),
        source_resolver=resolver,
        should_cancel=ctx.cancel.cancelled)

    status = (f"ok: {result['new']} new, {result['dups']} dup, "
              f"{result['errors']} error "
              f"({result['discovered']} discovered via "
              f"{'+'.join(result['methods'])}"
              f"{'; cancelled' if result['cancelled'] else ''})")
    await source_dao.set_poll_result(ctx.conn, source.id, status[:500])
    return status
