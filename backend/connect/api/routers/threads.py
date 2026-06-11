from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import psycopg

from connect.api.deps import get_db
from connect.domain.models import ThreadDetail
from connect.storage import events as event_dao

router = APIRouter(prefix="/threads", tags=["threads"])


@router.get("/{thread_id}", response_model=ThreadDetail)
async def get_thread(thread_id: int,
                     db: psycopg.AsyncConnection = Depends(get_db)):
    detail = await event_dao.get_thread_detail(db, thread_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return detail
