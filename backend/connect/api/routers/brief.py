from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response

from connect.api.deps import get_container
from connect.domain.models import BriefResponse
from connect.knowledge import briefing
from connect.orchestration.container import Container

router = APIRouter(prefix="/brief", tags=["brief"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/today", response_model=BriefResponse)
async def brief_today(container: Container = Depends(get_container)):
    """Today's brief — generated lazily on the first GET of the day, then
    persisted and stable all day."""
    brief = briefing.get_or_generate(container.db)
    assert brief is not None  # today always generates
    return brief


@router.get("/{brief_date}", response_model=BriefResponse)
async def brief_for_date(brief_date: str,
                         container: Container = Depends(get_container)):
    if not _DATE_RE.match(brief_date):
        raise HTTPException(status_code=422,
                            detail="date must be YYYY-MM-DD")
    brief = briefing.get_or_generate(container.db, brief_date)
    if brief is None:
        raise HTTPException(status_code=404,
                            detail=f"no brief for {brief_date}")
    return brief


@router.post("/items/{item_id}/seen", status_code=204,
             response_class=Response)
async def mark_item_seen(item_id: int,
                         container: Container = Depends(get_container)):
    if not briefing.mark_seen(container.db, item_id):
        raise HTTPException(status_code=404, detail="brief item not found")
    return Response(status_code=204)
