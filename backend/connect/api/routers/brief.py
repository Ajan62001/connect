from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response

import psycopg

from connect.api.deps import get_current_user, get_db
from connect.domain.models import BriefResponse, CurrentUser
from connect.knowledge import briefing

router = APIRouter(prefix="/brief", tags=["brief"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/today", response_model=BriefResponse)
async def brief_today(db: psycopg.AsyncConnection = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    """The caller's brief for today — generated lazily on their first GET
    of the day, then persisted and stable all day (per-user rows)."""
    brief = await briefing.get_or_generate(db, user.id)
    assert brief is not None  # today always generates
    return brief


@router.get("/{brief_date}", response_model=BriefResponse)
async def brief_for_date(brief_date: str,
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    if not _DATE_RE.match(brief_date):
        raise HTTPException(status_code=422,
                            detail="date must be YYYY-MM-DD")
    brief = await briefing.get_or_generate(db, user.id, brief_date)
    if brief is None:
        raise HTTPException(status_code=404,
                            detail=f"no brief for {brief_date}")
    return brief


@router.post("/items/{item_id}/seen", status_code=204,
             response_class=Response)
async def mark_item_seen(item_id: int,
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    if not await briefing.mark_seen(db, item_id, user.id):
        raise HTTPException(status_code=404, detail="brief item not found")
    return Response(status_code=204)
