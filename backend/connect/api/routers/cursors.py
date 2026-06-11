from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

import psycopg

from connect.api.deps import get_db
from connect.domain.enums import VIEW_SURFACES
from connect.domain.models import CursorCreate
from connect.storage import cursors as cursor_dao

router = APIRouter(prefix="/cursors", tags=["cursors"])


@router.post("", status_code=204, response_class=Response)
async def upsert_cursor(body: CursorCreate,
                        db: psycopg.AsyncConnection = Depends(get_db)):
    """Record "the user looked at this surface now" (last_seen_at=now)."""
    if body.surface not in VIEW_SURFACES:
        raise HTTPException(
            status_code=422,
            detail=f"surface must be one of {sorted(VIEW_SURFACES)}")
    await cursor_dao.upsert(db, body.surface, body.ref_id)
    return Response(status_code=204)
