"""Position-shift actions (v9) — dismiss for now; shifts are READ through
the per-topic views endpoint and the Today brief."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import psycopg

from connect.api.deps import get_db
from connect.domain.models import PositionShiftRow
from connect.storage import statements as statement_dao

router = APIRouter(prefix="/position-shifts", tags=["position-shifts"])


@router.post("/{shift_id}/dismiss", response_model=PositionShiftRow)
async def dismiss_shift(shift_id: int,
                        db: psycopg.AsyncConnection = Depends(get_db)):
    row = await statement_dao.dismiss_shift(db, shift_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="position shift not found")
    return row
