"""GET /api/spend — "my spend" plus the global context (tenancy design
§4, Phase D). The v0.1 fields (daily_cap_usd / today_spent_usd / days)
keep their meaning for older clients; ``mine`` carries the caller's
effective caps + ledger and ``global`` the deployment view (members see
the deployment numbers read-only — transparency, not control)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

import psycopg

from connect.api.deps import get_container, get_current_user, get_db
from connect.domain.models import CurrentUser, SpendDay, SpendReport, \
    SpendSlice
from connect.llm import spend as spend_mod
from connect.orchestration.container import Container
from connect.storage import app_settings as app_settings_dao

router = APIRouter(tags=["spend"])


@router.get("/spend", response_model=SpendReport)
async def spend(days: int = Query(default=7, ge=1, le=90),
                container: Container = Depends(get_container),
                db: psycopg.AsyncConnection = Depends(get_db),
                user: CurrentUser = Depends(get_current_user)):
    general = container.governor
    investigation = container.investigation_governor
    assert general is not None and investigation is not None
    effective = await app_settings_dao.effective_budgets(
        db, container.settings)
    global_days = [SpendDay(**d)
                   for d in await spend_mod.daily_breakdown(db, days)]
    today_total = await spend_mod.spent_today(db)
    return SpendReport(
        daily_cap_usd=container.settings.daily_llm_budget_usd,
        today_spent_usd=today_total,
        days=global_days,
        mine=SpendSlice(
            today_usd=await spend_mod.spent_today(db, user_id=user.id),
            cap_usd=await general.user_cap(db, user.id),
            investigation_cap_usd=await investigation.user_cap(db,
                                                               user.id),
            days=[SpendDay(**d)
                  for d in await spend_mod.daily_breakdown(
                      db, days, user_id=user.id)],
        ),
        global_=SpendSlice(
            today_usd=today_total,
            cap_usd=effective["global_daily_budget_usd"],
            investigation_cap_usd=effective[
                "investigation_daily_budget_usd"],
            days=global_days,
        ),
    )
