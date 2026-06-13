"""'story' — one story run, reconstructed from the self-describing payload
(seed + options round-trip their frozen models)."""

from __future__ import annotations

from typing import Any

from connect.story.schema import StoryOptions, StorySeed
from connect.workers.registry import WorkerContext, register


@register("story")
async def run_story(ctx: WorkerContext, payload: dict[str, Any]) -> None:
    service = ctx.services.stories
    assert service is not None, "StoryService not wired"
    seed = StorySeed(**payload["seed"])
    opts = StoryOptions(**payload["options"])
    return await service.run(ctx.conn, payload["dossier_id"], seed, opts,
                             job_id=ctx.job_id, cancel=ctx.cancel)
