from __future__ import annotations

from fastapi import APIRouter, Depends, Query

import psycopg

from connect.api.deps import get_db
from connect.domain.models import CalendarEntry
from connect.knowledge import calendar as calendar_logic

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get("", response_model=list[CalendarEntry])
async def upcoming_calendar(days: int = Query(default=120, ge=1, le=730),
                            db: psycopg.AsyncConnection = Depends(get_db)):
    rows = await calendar_logic.upcoming(db, days)
    return [CalendarEntry(id=r["id"], kind=r["kind"], scope=r["scope"],
                          occurs_on=r["occurs_on"], ends_on=r["ends_on"],
                          label=r["label"]) for r in rows]
