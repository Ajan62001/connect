"""The /api/stories surface — create a grounded story (202 + job), list,
snapshot detail, SSE event stream (job_event replay, ?after= / Last-Event-ID
resume), and cancel. Mirrors the analyses/investigations contract.

Tenancy: list/detail/SSE carry the owner-OR-shared predicate (404 otherwise);
create sets owner_id=:me; cancel is owner-or-admin.
"""

from __future__ import annotations

import psycopg
import pydantic
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from connect.api.deps import get_container, get_current_user, get_db
from connect.api.qa import grounded_answer
from connect.api.sse import job_event_stream
from connect.api.tenancy import require_owner_or_admin, resolve_dossier
from connect.domain.models import (
    CurrentUser,
    DocumentAnswer,
    DocumentAskRequest,
    JobAccepted,
)
from connect.llm.spend import BudgetExceeded
from connect.orchestration.container import Container
from connect.storage import stories as story_dao
from connect.story.schema import (
    StoryAccepted,
    StoryCreate,
    StoryDetail,
    StoryEdit,
    StoryPage,
    StorySeed,
)

_STORY_QA_SYSTEM = (
    "You answer the user's question about a STORY using ONLY the supplied"
    " facts (each a verbatim quote from a stored source) — never outside"
    " knowledge. If the facts do not answer it, set grounded=false and say"
    " it isn't covered. When grounded, quote a short verbatim excerpt from"
    " the facts that supports the answer. Be concise and specific.")

router = APIRouter(prefix="/stories", tags=["stories"])

MAX_PAGE_SIZE = 100


@router.post("", response_model=StoryAccepted, status_code=202)
async def create_story(body: StoryCreate,
                       container: Container = Depends(get_container),
                       user: CurrentUser = Depends(get_current_user)):
    if body.visibility not in (None, "private", "shared"):
        raise HTTPException(status_code=422,
                            detail="visibility must be private or shared")
    try:
        seed = StorySeed(
            story_id=body.story_id, investigation_id=body.investigation_id,
            workspace_id=body.workspace_id, topic=body.topic,
            since=body.since, until=body.until)
    except pydantic.ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    service = container.stories
    assert service is not None
    if service.provider is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    if container.jobs is None:
        raise HTTPException(status_code=503,
                            detail="background jobs are unavailable")
    try:
        await service.governor.check(0.10, user_id=user.id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        story_id, job_id = await service.start(
            seed, body.options, owner_id=user.id, visibility=body.visibility)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return StoryAccepted(story_id=story_id, job_id=job_id)


@router.get("", response_model=StoryPage)
async def list_stories(limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
                       offset: int = Query(default=0, ge=0),
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    return await story_dao.list_page(db, viewer=user.id, limit=limit,
                                     offset=offset)


@router.get("/{story_id}", response_model=StoryDetail)
async def get_story(story_id: int,
                    db: psycopg.AsyncConnection = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    detail = await story_dao.get_detail(db, story_id, viewer=user.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="story not found")
    return detail


def _translate(type_: str, data: dict) -> tuple[str, dict] | None:
    if type_ == "started":
        return None
    if type_ == "cancelled":
        return "error", {"message": "story cancelled"}
    if type_ == "error":
        return "error", {"message": (data.get("message") or data.get("error")
                                     or "story failed")}
    if type_ == "done":
        return "done", {}
    return type_, data


@router.get("/{story_id}/events")
async def story_events(story_id: int, request: Request,
                       after: int = Query(default=0, ge=0),
                       container: Container = Depends(get_container),
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    service = container.stories
    assert service is not None
    await resolve_dossier(db, story_id, "story", "story", user)
    job_id = await service.job_for(story_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="story has no job")
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    return EventSourceResponse(
        job_event_stream(request, container, job_id, after, _translate))


@router.patch("/{story_id}", response_model=StoryDetail)
async def edit_story(story_id: int, body: StoryEdit,
                     db: psycopg.AsyncConnection = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    """Hand-edit the generated narrative (owner-only)."""
    row = await resolve_dossier(db, story_id, "story", "story", user)
    require_owner_or_admin(row, user)
    fields = body.model_dump(exclude_unset=True)
    if fields:
        ok = await story_dao.update_narrative(
            db, story_id, owner_id=user.id, title=body.title,
            narrative_md=body.narrative_md)
        if not ok:
            raise HTTPException(status_code=409,
                                detail="story has no narrative to edit yet")
    detail = await story_dao.get_detail(db, story_id, viewer=user.id)
    assert detail is not None
    return detail


@router.post("/{story_id}/ask", response_model=DocumentAnswer)
async def ask_story(story_id: int, body: DocumentAskRequest,
                    container: Container = Depends(get_container),
                    db: psycopg.AsyncConnection = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    """Cross-question a story: a grounded answer drawn ONLY from the story's
    gathered facts."""
    await resolve_dossier(db, story_id, "story", "story", user)
    if container.llm is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is empty")
    context = await story_dao.fact_context(db, story_id)
    if context is None:
        raise HTTPException(status_code=409,
                            detail="this story has no facts to answer from yet")
    return await grounded_answer(
        container.llm, container.governor, db, system=_STORY_QA_SYSTEM,
        context=context, question=question, user_id=user.id,
        purpose="story_qa")


@router.post("/{story_id}/cancel", response_model=JobAccepted,
             status_code=202)
async def cancel_story(story_id: int,
                       container: Container = Depends(get_container),
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    service = container.stories
    assert service is not None
    row = await resolve_dossier(db, story_id, "story", "story", user)
    require_owner_or_admin(row, user)
    await service.cancel(story_id)
    job_id = await service.job_for(story_id) or 0
    return JobAccepted(job_id=job_id)
