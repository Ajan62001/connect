"""'investigation' — one investigation run, reconstructed from the
self-describing payload (seed + options round-trip their frozen models)."""

from __future__ import annotations

from typing import Any

from connect.investigation.schema import (
    InvestigationOptions,
    InvestigationSeed,
)
from connect.workers.registry import WorkerContext, register


@register("investigation")
async def run_investigation(ctx: WorkerContext,
                            payload: dict[str, Any]) -> None:
    service = ctx.services.investigations
    assert service is not None, "InvestigationService not wired"
    seed = InvestigationSeed(**payload["seed"])
    opts = InvestigationOptions(**payload["options"])
    return await service.run(ctx.conn, payload["dossier_id"], seed, opts,
                             job_id=ctx.job_id, cancel=ctx.cancel)
