"""The /api/analyses surface — create (202 + job), list, snapshot detail,
SSE event stream (replays job_event rows by seq, honors ?after= and the
Last-Event-ID header, then live-streams — bus pings + fallback re-query —
until a terminal event), cancel, visibility PATCH (share cascade).

Client reconnect strategy per contract: refetch the snapshot (GET
/api/analyses/{id} carries last_seq), then resume the stream with ?after=.

Tenancy (design §5): list/detail/SSE carry the ``owner OR shared``
predicate (404 otherwise); create sets owner_id=:me (+ optional private
toggle); cancel and visibility-PATCH are owner-or-admin.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from connect.api.deps import get_container, get_current_user, get_db
from connect.api.limits import check_interactive_capacity
from connect.api.sse import job_event_stream
from connect.api.tenancy import patch_visibility, require_owner_or_admin, \
    resolve_dossier
from connect.domain.models import (
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisDetail,
    AnalysisPage,
    CurrentUser,
    DossierVisibilityResult,
    DossierVisibilityUpdate,
    JobAccepted,
)
from connect.llm.spend import BudgetExceeded
from connect.orchestration.container import Container
from connect.storage import analyses as analysis_dao

router = APIRouter(prefix="/analyses", tags=["analyses"])

MAX_PAGE_SIZE = 100

# pre-accept budget projection: roughly the first normalize call — enough
# to refuse an over-budget day upfront with a friendly 429 instead of a
# failed job (the in-job governor checks remain the per-call truth)
FIRST_CALL_PROJECTION_USD = 0.05


@router.post("", response_model=AnalysisAccepted, status_code=202)
async def create_analysis(body: AnalysisCreate,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    service = container.analysis
    assert service is not None
    if service.provider is None:
        raise HTTPException(status_code=503,
                            detail="ANTHROPIC_API_KEY not set")
    input_text = body.input_text.strip()
    if not input_text:
        raise HTTPException(status_code=422, detail="input_text is empty")
    # Phase D backpressure + tenant budget gate (both → friendly 429)
    await check_interactive_capacity(db, container.settings, user.id)
    try:
        await container.governor.check(
            min(container.settings.analysis_budget_usd,
                FIRST_CALL_PROJECTION_USD), user_id=user.id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    analysis_id, job_id = await service.start(
        input_text,
        owner_id=user.id,
        visibility=body.visibility or "shared",
        max_evidence_per_claim=body.options.max_evidence_per_claim)
    return AnalysisAccepted(analysis_id=analysis_id, job_id=job_id)


@router.get("", response_model=AnalysisPage)
async def list_analyses(page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=20, ge=1,
                                               le=MAX_PAGE_SIZE),
                        db: psycopg.AsyncConnection = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    return await analysis_dao.list_page(db, page=page,
                                        page_size=page_size,
                                        viewer=user.id)


@router.get("/{analysis_id}", response_model=AnalysisDetail)
async def get_analysis(analysis_id: int,
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    detail = await analysis_dao.get_detail(db, analysis_id, viewer=user.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="analysis not found")
    return detail


@router.patch("/{analysis_id}", response_model=DossierVisibilityResult)
async def patch_analysis(analysis_id: int, body: DossierVisibilityUpdate,
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    """Visibility flip with the share cascade (private->shared auto-shares
    cited private docs after confirmation; shared->private is 409)."""
    return await patch_visibility(db, analysis_id, "analysis", "analysis",
                                  user, body)


@router.post("/{analysis_id}/cancel", response_model=JobAccepted,
             status_code=202)
async def cancel_analysis(analysis_id: int,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    service = container.analysis
    assert service is not None
    row = await resolve_dossier(db, analysis_id, "analysis", "analysis",
                                user)
    require_owner_or_admin(row, user)
    await service.cancel(analysis_id)  # no-op on already-terminal analyses
    job_id = await service.job_for(analysis_id) or 0
    return JobAccepted(job_id=job_id)


def _translate(type_: str, data: dict) -> tuple[str, dict] | None:
    """job_event rows -> contract event shapes. The queue's generic rows
    are mapped ('error' {error} -> error {message}; 'cancelled' -> error;
    'started' is dropped); pipeline-written rows pass through."""
    if type_ == "started":
        return None
    if type_ == "cancelled":
        return "error", {"message": "analysis cancelled"}
    if type_ == "error":
        message = data.get("message") or data.get("error") \
            or "analysis failed"
        return "error", {"message": message}
    if type_ == "done":
        return "done", {}
    return type_, data


@router.get("/{analysis_id}/events")
async def analysis_events(analysis_id: int, request: Request,
                          after: int = Query(default=0, ge=0),
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    service = container.analysis
    assert service is not None
    # the read-predicate guard sits on the route BEFORE the stream starts
    await resolve_dossier(db, analysis_id, "analysis", "analysis", user)
    job_id = await service.job_for(analysis_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="analysis has no job")

    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    return EventSourceResponse(
        job_event_stream(request, container, job_id, after, _translate))
