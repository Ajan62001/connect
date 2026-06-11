"""The /api/investigations surface — create (202 + job, 429 on the
SEPARATE investigation governor), list with counts, snapshot detail, SSE
event stream (job_event replay by seq, ?after= + Last-Event-ID resume),
cancel, and POST /api/questions/{id}/investigate (manual recursion: the
child dossier carries parent_question_id, the question gains
spawned_dossier_id). Mirrors the Phase-3 analyses contract exactly.
"""

from __future__ import annotations

import asyncio
import json

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from connect.api.deps import get_container
from connect.domain.models import JobAccepted
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
TERMINAL_TYPES = ("done", "error", "cancelled")
POLL_SECONDS = 0.2


def _start(container: Container, seed: InvestigationSeed,
           options: InvestigationOptions | None) -> InvestigationAccepted:
    service = container.investigations
    assert service is not None
    if service.provider is None:
        raise HTTPException(status_code=503,
                            detail="ANTHROPIC_API_KEY not set")
    opts = options or service.default_options()
    try:
        container.investigation_governor.check(
            min(opts.budget_usd, 0.10))   # projected first-call spend
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        investigation_id, job_id = service.start(seed, opts)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return InvestigationAccepted(investigation_id=investigation_id,
                                 job_id=job_id)


@router.post("/investigations", response_model=InvestigationAccepted,
             status_code=202)
async def create_investigation(body: InvestigationCreate,
                               container: Container = Depends(
                                   get_container)):
    try:
        seed = InvestigationSeed(
            topic=body.topic, entity_id=body.entity_id,
            event_id=body.event_id, story_id=body.story_id)
    except pydantic.ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _start(container, seed, body.options)


@router.post("/questions/{question_id}/investigate",
             response_model=InvestigationAccepted, status_code=202)
async def investigate_question(question_id: int,
                               options: InvestigationOptions | None = None,
                               container: Container = Depends(
                                   get_container)):
    """Manual recursion — spawn a child investigation from a question."""
    row = container.db.execute("SELECT id FROM question WHERE id = ?",
                               (question_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="question not found")
    seed = InvestigationSeed(question_id=question_id)
    return _start(container, seed, options)


@router.get("/investigations", response_model=InvestigationPage)
async def list_investigations(page: int = Query(default=1, ge=1),
                              page_size: int = Query(default=20, ge=1,
                                                     le=MAX_PAGE_SIZE),
                              container: Container = Depends(
                                  get_container)):
    return investigation_dao.list_page(container.db, page=page,
                                       page_size=page_size)


@router.get("/investigations/{investigation_id}",
            response_model=InvestigationDetail)
async def get_investigation(investigation_id: int,
                            container: Container = Depends(get_container)):
    detail = investigation_dao.get_detail(container.db, investigation_id)
    if detail is None:
        raise HTTPException(status_code=404,
                            detail="investigation not found")
    return detail


@router.post("/investigations/{investigation_id}/cancel",
             response_model=JobAccepted, status_code=202)
async def cancel_investigation(investigation_id: int,
                               container: Container = Depends(
                                   get_container)):
    service = container.investigations
    assert service is not None
    row = container.db.execute(
        "SELECT id FROM dossier WHERE id = ? AND kind = 'investigation'",
        (investigation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="investigation not found")
    service.cancel(investigation_id)  # no-op on terminal investigations
    job_id = service.job_for(investigation_id) or 0
    return JobAccepted(job_id=job_id)


def _translate(type_: str, data: dict) -> tuple[str, dict] | None:
    """job_event rows -> contract event shapes (JobRunner generic rows are
    mapped; investigation-written rows pass through)."""
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
                                   get_container)):
    service = container.investigations
    assert service is not None
    conn = container.db
    if conn.execute(
            "SELECT 1 FROM dossier WHERE id = ? AND kind = 'investigation'",
            (investigation_id,)).fetchone() is None:
        raise HTTPException(status_code=404,
                            detail="investigation not found")
    job_id = service.job_for(investigation_id)
    if job_id is None:
        raise HTTPException(status_code=404,
                            detail="investigation has no job")

    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    async def stream():
        last = after
        idle_terminal_polls = 0
        while True:
            rows = conn.execute(
                "SELECT seq, type, data FROM job_event"
                " WHERE job_id = ? AND seq > ? ORDER BY seq",
                (job_id, last)).fetchall()
            terminal = False
            for row in rows:
                last = int(row["seq"])
                try:
                    data = json.loads(row["data"] or "{}")
                except (ValueError, TypeError):
                    data = {}
                translated = _translate(row["type"], data)
                if row["type"] in TERMINAL_TYPES:
                    terminal = True
                if translated is None:
                    continue
                event, payload = translated
                yield {"id": str(row["seq"]), "event": event,
                       "data": json.dumps(payload)}
            if terminal:
                break
            # safety valve: job row already terminal (e.g. replay of an
            # orphaned job whose terminal event never landed)
            status_row = conn.execute(
                "SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()
            if status_row and status_row["status"] in ("done", "failed",
                                                       "cancelled"):
                idle_terminal_polls += 1
                if idle_terminal_polls >= 2 and not rows:
                    break
            if await request.is_disconnected():
                break
            await asyncio.sleep(POLL_SECONDS)

    return EventSourceResponse(stream())
