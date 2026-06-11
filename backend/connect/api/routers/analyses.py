"""The /api/analyses surface — create (202 + job), list, snapshot detail,
SSE event stream (replays job_event rows by seq, honors ?after= and the
Last-Event-ID header, then live-streams until a terminal event), cancel.

Client reconnect strategy per contract: refetch the snapshot (GET
/api/analyses/{id} carries last_seq), then resume the stream with ?after=.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from connect.api.deps import get_container
from connect.domain.models import (
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisDetail,
    AnalysisPage,
    JobAccepted,
)
from connect.orchestration.container import Container
from connect.storage import analyses as analysis_dao

router = APIRouter(prefix="/analyses", tags=["analyses"])

MAX_PAGE_SIZE = 100
TERMINAL_TYPES = ("done", "error", "cancelled")
POLL_SECONDS = 0.2


@router.post("", response_model=AnalysisAccepted, status_code=202)
async def create_analysis(body: AnalysisCreate,
                          container: Container = Depends(get_container)):
    service = container.analysis
    assert service is not None
    if service.provider is None:
        raise HTTPException(status_code=503,
                            detail="ANTHROPIC_API_KEY not set")
    input_text = body.input_text.strip()
    if not input_text:
        raise HTTPException(status_code=422, detail="input_text is empty")
    analysis_id, job_id = service.start(
        input_text,
        max_evidence_per_claim=body.options.max_evidence_per_claim)
    return AnalysisAccepted(analysis_id=analysis_id, job_id=job_id)


@router.get("", response_model=AnalysisPage)
async def list_analyses(page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=20, ge=1,
                                               le=MAX_PAGE_SIZE),
                        container: Container = Depends(get_container)):
    return analysis_dao.list_page(container.db, page=page,
                                  page_size=page_size)


@router.get("/{analysis_id}", response_model=AnalysisDetail)
async def get_analysis(analysis_id: int,
                       container: Container = Depends(get_container)):
    detail = analysis_dao.get_detail(container.db, analysis_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="analysis not found")
    return detail


@router.post("/{analysis_id}/cancel", response_model=JobAccepted,
             status_code=202)
async def cancel_analysis(analysis_id: int,
                          container: Container = Depends(get_container)):
    service = container.analysis
    assert service is not None
    row = container.db.execute("SELECT id FROM dossier WHERE id = ?",
                               (analysis_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="analysis not found")
    service.cancel(analysis_id)  # no-op on already-terminal analyses
    job_id = service.job_for(analysis_id) or 0
    return JobAccepted(job_id=job_id)


def _translate(type_: str, data: dict) -> tuple[str, dict] | None:
    """job_event rows -> contract event shapes. JobRunner's generic rows
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
                          container: Container = Depends(get_container)):
    service = container.analysis
    assert service is not None
    conn = container.db
    if conn.execute("SELECT 1 FROM dossier WHERE id = ?",
                    (analysis_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="analysis not found")
    job_id = service.job_for(analysis_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="analysis has no job")

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
