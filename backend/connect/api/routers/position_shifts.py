"""Position-shift actions (v9) — dismiss for now; shifts are READ through
the per-topic views endpoint and the Today brief."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from connect.api.deps import get_container
from connect.domain.models import PositionShiftRow
from connect.orchestration.container import Container
from connect.storage import statements as statement_dao

router = APIRouter(prefix="/position-shifts", tags=["position-shifts"])


@router.post("/{shift_id}/dismiss", response_model=PositionShiftRow)
async def dismiss_shift(shift_id: int,
                        container: Container = Depends(get_container)):
    row = statement_dao.dismiss_shift(container.db, shift_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="position shift not found")
    return row
