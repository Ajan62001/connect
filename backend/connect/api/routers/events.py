from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import psycopg

from connect.api.deps import get_db
from connect.domain.models import EventDetail
from connect.storage import events as event_dao

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/{event_id}", response_model=EventDetail)
async def get_event(event_id: int,
                    db: psycopg.AsyncConnection = Depends(get_db)):
    detail = await event_dao.get_event_detail(db, event_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="event not found")
    return detail
