from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from connect.api.deps import get_container
from connect.domain.models import SpendDay, SpendReport
from connect.llm import spend as spend_mod
from connect.orchestration.container import Container

router = APIRouter(tags=["spend"])


@router.get("/spend", response_model=SpendReport)
async def spend(days: int = Query(default=7, ge=1, le=90),
                container: Container = Depends(get_container)):
    conn = container.db
    return SpendReport(
        daily_cap_usd=container.settings.daily_llm_budget_usd,
        today_spent_usd=spend_mod.spent_today(conn),
        days=[SpendDay(**d) for d in spend_mod.daily_breakdown(conn, days)],
    )
