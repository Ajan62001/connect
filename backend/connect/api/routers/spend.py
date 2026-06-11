from __future__ import annotations

from fastapi import APIRouter, Depends, Query

import psycopg

from connect.api.deps import get_container, get_db
from connect.domain.models import SpendDay, SpendReport
from connect.llm import spend as spend_mod
from connect.orchestration.container import Container

router = APIRouter(tags=["spend"])


@router.get("/spend", response_model=SpendReport)
async def spend(days: int = Query(default=7, ge=1, le=90),
                container: Container = Depends(get_container),
                db: psycopg.AsyncConnection = Depends(get_db)):
    return SpendReport(
        daily_cap_usd=container.settings.daily_llm_budget_usd,
        today_spent_usd=await spend_mod.spent_today(db),
        days=[SpendDay(**d)
              for d in await spend_mod.daily_breakdown(db, days)],
    )
