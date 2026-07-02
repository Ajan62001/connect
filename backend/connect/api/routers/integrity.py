"""Admin integrity dashboard (S5) — per-day rates over the integrity_event
stream plus the latest live-eval runs."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Query

from connect.api.deps import get_db, require_admin
from connect.integrity import dashboard

router = APIRouter(prefix="/integrity", tags=["integrity"])


@router.get("/dashboard", dependencies=[Depends(require_admin)])
async def integrity_dashboard(
        days: int = Query(default=14, ge=1, le=90),
        db: psycopg.AsyncConnection = Depends(get_db)):
    """Grounding / gate-flag / contested rates per day, the verdict
    distribution, and recent live-eval runs. Admin-only."""
    return await dashboard.integrity_breakdown(db, days=days)
