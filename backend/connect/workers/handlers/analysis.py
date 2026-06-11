"""'analysis' — one Phase-3 claim analysis (the dossier row already exists;
the payload carries what start() resolved)."""

from __future__ import annotations

from typing import Any

from connect.workers.registry import WorkerContext, register


@register("analysis")
async def run_analysis(ctx: WorkerContext, payload: dict[str, Any]) -> None:
    service = ctx.services.analysis
    assert service is not None, "AnalysisService not wired"
    return await service.run(
        ctx.conn, payload["dossier_id"],
        payload.get("max_evidence_per_claim"),
        job_id=ctx.job_id, cancel=ctx.cancel)
