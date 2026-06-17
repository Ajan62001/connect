"""'content_generate' / 'content_publish' — the content-pipeline job bodies,
reconstructed from their self-describing payloads."""

from __future__ import annotations

from typing import Any

from connect.content.schema import ContentOptions, ContentSeed
from connect.workers.registry import WorkerContext, register


@register("content_generate")
async def run_content_generate(ctx: WorkerContext,
                               payload: dict[str, Any]) -> Any:
    service = ctx.services.content
    assert service is not None, "ContentService not wired"
    seed = ContentSeed(**payload["seed"])
    opts = ContentOptions(**payload["options"])
    return await service.run(
        ctx.conn, payload["campaign_id"], seed, list(payload["formats"]),
        opts, job_id=ctx.job_id, cancel=ctx.cancel)


@register("content_publish")
async def run_content_publish(ctx: WorkerContext,
                              payload: dict[str, Any]) -> Any:
    service = ctx.services.content
    assert service is not None, "ContentService not wired"
    return await service.publish_item(
        ctx.conn, int(payload["item_id"]),
        target=str(payload.get("target") or "direct"))


@register("content_render")
async def run_content_render(ctx: WorkerContext,
                             payload: dict[str, Any]) -> Any:
    """Re-render an edited item's media (e.g. a reel after a script/voice edit)."""
    service = ctx.services.content
    assert service is not None, "ContentService not wired"
    return await service.rerender(
        ctx.conn, int(payload["item_id"]),
        ContentOptions(**(payload.get("options") or {})),
        job_id=ctx.job_id, owner_id=int(payload["owner_id"]))
