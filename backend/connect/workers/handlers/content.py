"""'content_generate' / 'content_publish' — the content-pipeline job bodies,
reconstructed from their self-describing payloads."""

from __future__ import annotations

from typing import Any

from connect.content.schema import ContentOptions, ContentSeed
from connect.workers.registry import WorkerContext, register


def _options(raw: dict[str, Any] | None) -> ContentOptions:
    """Job payloads are durable DB rows that outlive deploys: a payload written
    by a newer (or older) build may carry option keys this build doesn't know,
    and ContentOptions is extra='forbid' — reconstructing it verbatim fails the
    whole job on version skew (seen live: the api enqueued ``visual_style``
    before the worker was restarted). Unknown keys are dropped instead."""
    return ContentOptions(**{k: v for k, v in (raw or {}).items()
                             if k in ContentOptions.model_fields})


@register("content_generate")
async def run_content_generate(ctx: WorkerContext,
                               payload: dict[str, Any]) -> Any:
    service = ctx.services.content
    assert service is not None, "ContentService not wired"
    seed = ContentSeed(**payload["seed"])
    opts = _options(payload["options"])
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


@register("content_verify")
async def run_content_verify(ctx: WorkerContext,
                            payload: dict[str, Any]) -> Any:
    """S2 editorial gate: re-verify a stored item against its provenance quotes,
    persist the refreshed report, and (enforcing mode) promote it out of the
    'verifying' holding state."""
    service = ctx.services.content
    assert service is not None, "ContentService not wired"
    return await service.verify_item(
        ctx.conn, int(payload["item_id"]),
        promote_to=str(payload.get("promote_to") or "approved"))


@register("content_render")
async def run_content_render(ctx: WorkerContext,
                             payload: dict[str, Any]) -> Any:
    """Re-render an edited item's media (e.g. a reel after a script/voice edit)."""
    service = ctx.services.content
    assert service is not None, "ContentService not wired"
    return await service.rerender(
        ctx.conn, int(payload["item_id"]),
        _options(payload.get("options")),
        job_id=ctx.job_id, owner_id=int(payload["owner_id"]))


@register("reel_factory")
async def run_reel_factory(ctx: WorkerContext,
                           payload: dict[str, Any]) -> Any:
    """One factory run: scout hot subjects from the feed and commission one
    reel-led campaign per pick (the campaigns then generate/render in their
    own content_generate jobs)."""
    service = ctx.services.reel_factory
    assert service is not None, "ReelFactoryService not wired"
    return await service.run(
        ctx.conn, payload, job_id=ctx.job_id,
        owner_id=int(payload["owner_id"]), cancel=ctx.cancel)
