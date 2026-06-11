"""'poll_source' — one poll cycle for one source. The payload carries only
source_id; the handler reconstructs the Source row (a vanished row fails
the job — visible, never silent)."""

from __future__ import annotations

from typing import Any

from connect.storage import sources as source_dao
from connect.workers.registry import WorkerContext, register


@register("poll_source")
async def poll_source(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    poller = ctx.services.poller
    assert poller is not None, "SourcePoller not wired"
    source = await source_dao.get(ctx.conn, payload["source_id"])
    if source is None:
        raise LookupError(f"source {payload['source_id']} not found")
    return await poller.poll_source(ctx.conn, source)
