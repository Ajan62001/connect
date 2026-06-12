"""The /api/investigations surface — create (202 + job, 429 on the
SEPARATE investigation governor), list with counts, snapshot detail, SSE
event stream (job_event replay by seq, ?after= + Last-Event-ID resume),
cancel, visibility PATCH (share cascade + deferred-edge materialization),
and POST /api/questions/{id}/investigate (manual recursion: the child
dossier carries parent_question_id, the question gains
spawned_dossier_id). Mirrors the Phase-3 analyses contract exactly.

Tenancy (design §5): list/detail/SSE carry the ``owner OR shared``
predicate (404 otherwise); create sets owner_id=:me (child investigations
of a private dossier's question default private); cancel and
visibility-PATCH are owner-or-admin.
"""

from __future__ import annotations

import psycopg
import pydantic
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from connect.api.deps import get_container, get_current_user, get_db
from connect.api.limits import check_interactive_capacity
from connect.api.sse import job_event_stream
from connect.api.tenancy import patch_visibility, require_owner_or_admin, \
    resolve_dossier
from connect.domain.models import (
    CurrentUser,
    DossierVisibilityResult,
    DossierVisibilityUpdate,
    JobAccepted,
)
from connect.investigation.schema import (
    InvestigationAccepted,
    InvestigationCreate,
    InvestigationDetail,
    InvestigationOptions,
    InvestigationPage,
    InvestigationSeed,
)
from connect.llm.spend import BudgetExceeded
from connect.orchestration.container import Container
from connect.storage import investigations as investigation_dao

router = APIRouter(tags=["investigations"])

MAX_PAGE_SIZE = 100


async def _start(container: Container, seed: InvestigationSeed,
                 options: InvestigationOptions | None, *,
                 owner_id: int, visibility: str | None = None,
                 ) -> InvestigationAccepted:
    service = container.investigations
    assert service is not None
    if service.provider is None:
        raise HTTPException(status_code=503,
                            detail="ANTHROPIC_API_KEY not set")
    opts = options or service.default_options()
    # Phase D backpressure (per-user in-flight + global queue depth)
    async with container.pool.connection() as conn:
        await check_interactive_capacity(conn, container.settings,
                                         owner_id)
    try:
        await container.investigation_governor.check(
            min(opts.budget_usd, 0.10),   # projected first-call spend
            user_id=owner_id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        investigation_id, job_id = await service.start(
            seed, opts, owner_id=owner_id, visibility=visibility)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return InvestigationAccepted(investigation_id=investigation_id,
                                 job_id=job_id)


@router.post("/investigations", response_model=InvestigationAccepted,
             status_code=202)
async def create_investigation(body: InvestigationCreate,
                               container: Container = Depends(
                                   get_container),
                               user: CurrentUser = Depends(
                                   get_current_user)):
    if body.visibility not in (None, "private", "shared"):
        raise HTTPException(status_code=422,
                            detail="visibility must be private or shared")
    try:
        seed = InvestigationSeed(
            topic=body.topic, entity_id=body.entity_id,
            event_id=body.event_id, story_id=body.story_id)
    except pydantic.ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return await _start(container, seed, body.options,
                        owner_id=user.id, visibility=body.visibility)


@router.post("/questions/{question_id}/investigate",
             response_model=InvestigationAccepted, status_code=202)
async def investigate_question(question_id: int,
                               options: InvestigationOptions | None = None,
                               container: Container = Depends(
                                   get_container),
                               db: psycopg.AsyncConnection = Depends(
                                   get_db),
                               user: CurrentUser = Depends(
                                   get_current_user)):
    """Manual recursion — spawn a child investigation from a question.
    The question's dossier must be visible to the caller; a private
    parent's child inherits 'private' (runner.start)."""
    cur = await db.execute(
        "SELECT q.id FROM question q JOIN dossier d ON d.id = q.dossier_id"
        " WHERE q.id = %s AND (d.owner_id = %s OR d.visibility = 'shared')",
        (question_id, user.id))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="question not found")
    seed = InvestigationSeed(question_id=question_id)
    return await _start(container, seed, options, owner_id=user.id)


@router.get("/investigations", response_model=InvestigationPage)
async def list_investigations(page: int = Query(default=1, ge=1),
                              page_size: int = Query(default=20, ge=1,
                                                     le=MAX_PAGE_SIZE),
                              db: psycopg.AsyncConnection = Depends(
                                  get_db),
                              user: CurrentUser = Depends(
                                  get_current_user)):
    return await investigation_dao.list_page(db, page=page,
                                             page_size=page_size,
                                             viewer=user.id)


@router.get("/investigations/{investigation_id}",
            response_model=InvestigationDetail)
async def get_investigation(investigation_id: int,
                            db: psycopg.AsyncConnection = Depends(get_db),
                            user: CurrentUser = Depends(get_current_user)):
    detail = await investigation_dao.get_detail(db, investigation_id,
                                                viewer=user.id)
    if detail is None:
        raise HTTPException(status_code=404,
                            detail="investigation not found")
    return detail


@router.patch("/investigations/{investigation_id}",
              response_model=DossierVisibilityResult)
async def patch_investigation(investigation_id: int,
                              body: DossierVisibilityUpdate,
                              db: psycopg.AsyncConnection = Depends(get_db),
                              user: CurrentUser = Depends(
                                  get_current_user)):
    """Visibility flip with the share cascade: private->shared auto-shares
    cited private docs (after confirmation) and materializes the deferred
    grade-2 edges; shared->private is 409."""
    return await patch_visibility(db, investigation_id, "investigation",
                                  "investigation", user, body)


@router.post("/investigations/{investigation_id}/cancel",
             response_model=JobAccepted, status_code=202)
async def cancel_investigation(investigation_id: int,
                               container: Container = Depends(
                                   get_container),
                               db: psycopg.AsyncConnection = Depends(
                                   get_db),
                               user: CurrentUser = Depends(
                                   get_current_user)):
    service = container.investigations
    assert service is not None
    row = await resolve_dossier(db, investigation_id, "investigation",
                                "investigation", user)
    require_owner_or_admin(row, user)
    await service.cancel(investigation_id)  # no-op on terminal runs
    job_id = await service.job_for(investigation_id) or 0
    return JobAccepted(job_id=job_id)


def _translate(type_: str, data: dict) -> tuple[str, dict] | None:
    """job_event rows -> contract event shapes (the queue's generic rows
    are mapped; investigation-written rows pass through)."""
    if type_ == "started":
        return None
    if type_ == "cancelled":
        return "error", {"message": "investigation cancelled"}
    if type_ == "error":
        message = data.get("message") or data.get("error") \
            or "investigation failed"
        return "error", {"message": message}
    if type_ == "done":
        return "done", {}
    return type_, data


@router.get("/investigations/{investigation_id}/events")
async def investigation_events(investigation_id: int, request: Request,
                               after: int = Query(default=0, ge=0),
                               container: Container = Depends(
                                   get_container),
                               db: psycopg.AsyncConnection = Depends(
                                   get_db),
                               user: CurrentUser = Depends(
                                   get_current_user)):
    service = container.investigations
    assert service is not None
    # the read-predicate guard sits on the route BEFORE the stream starts
    await resolve_dossier(db, investigation_id, "investigation",
                          "investigation", user)
    job_id = await service.job_for(investigation_id)
    if job_id is None:
        raise HTTPException(status_code=404,
                            detail="investigation has no job")

    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    return EventSourceResponse(
        job_event_stream(request, container, job_id, after, _translate))
