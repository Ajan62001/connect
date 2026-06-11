from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from connect.api.deps import get_container
from connect.domain.models import EnrichmentSweepRequest, JobAccepted
from connect.orchestration.container import Container

router = APIRouter(prefix="/enrichment", tags=["enrichment"])


@router.post("/sweep", response_model=JobAccepted, status_code=202)
async def sweep(body: EnrichmentSweepRequest,
                container: Container = Depends(get_container)):
    """Queue one T1 sweep job. 'sync' processes eligible docs inline in the
    job (governor-checked per call); 'batch' submits one Anthropic Message
    Batch and the job polls/ingests the results."""
    service = container.enrichment
    jobs = container.jobs
    assert service is not None and jobs is not None
    if service.provider is None:
        raise HTTPException(status_code=503,
                            detail="ANTHROPIC_API_KEY not set")
    limit = body.limit
    target = body.target  # 'statements' = the pre-t1-v2 backfill
    kind = "enrich_t1_sync" if body.mode == "sync" else "enrich_t1_batch"
    job_id = await jobs.enqueue(kind, {"mode": body.mode, "limit": limit,
                                       "target": target})
    return JobAccepted(job_id=job_id)
