"""The /api/admin surface (tenancy design §5, Phase D) — users
(role/disable/budget overrides), the invite allowlist (Phase B), the
admin-editable budget settings, and the system spend breakdown.

Everything here is behind require_admin (admin-only invites is a locked
decision; private work stays absolutely private — nothing in this router
reads member dossiers or documents, only identity rows and the spend
ledger)."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from connect.api.deps import get_container, get_current_user, get_db, \
    require_admin
from connect.domain.models import (
    AdminSettings,
    AdminSettingsUpdate,
    AdminSpend,
    AdminSpendUser,
    AdminUser,
    AdminUserUpdate,
    CurrentUser,
    Invite,
    InviteCreate,
    SpendDay,
)
from connect.llm import spend as spend_mod
from connect.orchestration.container import Container
from connect.storage import app_settings as app_settings_dao
from connect.storage import invites as invite_dao
from connect.storage import sessions as session_dao
from connect.storage import users as user_dao

router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])

MAX_SPEND_DAYS = 90


# --- users ---------------------------------------------------------------------

@router.get("/users", response_model=list[AdminUser])
async def list_users(db: psycopg.AsyncConnection = Depends(get_db)):
    return [user_dao.to_admin_model(r) for r in await user_dao.list_all(db)]


@router.get("/users/{user_id}", response_model=AdminUser)
async def get_user(user_id: int,
                   db: psycopg.AsyncConnection = Depends(get_db)):
    row = await user_dao.get(db, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user_dao.to_admin_model(row)


@router.patch("/users/{user_id}", response_model=AdminUser)
async def patch_user(user_id: int, body: AdminUserUpdate,
                     caller: CurrentUser = Depends(get_current_user),
                     db: psycopg.AsyncConnection = Depends(get_db)):
    """Role / disable / budget-override management. Absent fields stay
    untouched; an explicit null budget clears the override back to the
    role default. Disabling a user revokes every session immediately
    (server-side sessions — design §3's instant-revocation payoff)."""
    changes: dict[str, object] = {
        field: getattr(body, field)
        for field in body.model_fields_set
        if field in user_dao.ADMIN_MUTABLE_COLUMNS
    }
    if not changes:
        raise HTTPException(status_code=422, detail="no changes provided")
    if "role" in changes and changes["role"] is None:
        raise HTTPException(status_code=422, detail="role cannot be null")
    if "disabled" in changes and changes["disabled"] is None:
        raise HTTPException(status_code=422,
                            detail="disabled cannot be null")
    # lockout guard: admins cannot demote or disable THEMSELVES (another
    # admin can — recovery stays possible via CONNECT_ADMIN_EMAILS)
    if user_id == caller.id and (changes.get("role") == "member"
                                 or changes.get("disabled") is True):
        raise HTTPException(
            status_code=409,
            detail="you cannot demote or disable your own account")
    row = await user_dao.admin_update(db, user_id, changes)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    if changes.get("disabled") is True:
        await session_dao.delete_for_user(db, user_id)
    return user_dao.to_admin_model(row)


# --- invites (Phase B) -----------------------------------------------------------

@router.get("/invites", response_model=list[Invite])
async def list_invites(db: psycopg.AsyncConnection = Depends(get_db)):
    return [invite_dao.to_model(r) for r in await invite_dao.list_all(db)]


@router.post("/invites", response_model=Invite, status_code=201)
async def create_invite(body: InviteCreate,
                        user: CurrentUser = Depends(get_current_user),
                        db: psycopg.AsyncConnection = Depends(get_db)):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="invalid email")
    row = await invite_dao.insert(db, email=email, invited_by=user.id,
                                  note=body.note)
    if row is None:
        raise HTTPException(status_code=409,
                            detail=f"{email} is already invited")
    return invite_dao.to_model(row)


@router.delete("/invites/{email}", status_code=204, response_class=Response)
async def delete_invite(email: str,
                        db: psycopg.AsyncConnection = Depends(get_db)):
    if not await invite_dao.delete(db, email):
        raise HTTPException(status_code=404, detail="invite not found")
    return Response(status_code=204)


# --- settings (admin-editable budget globals) --------------------------------------

@router.get("/settings", response_model=AdminSettings)
async def get_settings(container: Container = Depends(get_container),
                       db: psycopg.AsyncConnection = Depends(get_db)):
    effective = await app_settings_dao.effective_budgets(
        db, container.settings)
    return AdminSettings(**effective)


@router.patch("/settings", response_model=AdminSettings)
async def patch_settings(body: AdminSettingsUpdate,
                         container: Container = Depends(get_container),
                         db: psycopg.AsyncConnection = Depends(get_db)):
    """Upsert the provided keys into app_setting. The TenantGovernors read
    these per check, so the change is live in every process immediately —
    no restart, no in-memory propagation."""
    changes = {field: getattr(body, field)
               for field in body.model_fields_set
               if getattr(body, field) is not None}
    if not changes:
        raise HTTPException(status_code=422, detail="no changes provided")
    for key, value in changes.items():
        await app_settings_dao.set_value(db, key, str(float(value)))
    effective = await app_settings_dao.effective_budgets(
        db, container.settings)
    return AdminSettings(**effective)


# --- spend (system totals + per-user breakdown) -------------------------------------

@router.get("/spend", response_model=AdminSpend)
async def admin_spend(days: int = Query(default=7, ge=1,
                                        le=MAX_SPEND_DAYS),
                      container: Container = Depends(get_container),
                      db: psycopg.AsyncConnection = Depends(get_db)):
    effective = await app_settings_dao.effective_budgets(
        db, container.settings)
    general = container.governor
    investigation = container.investigation_governor
    assert general is not None and investigation is not None

    per_user = await spend_mod.per_user_daily_breakdown(db, days)
    rows_by_id = {int(r["id"]): r for r in await user_dao.list_all(db)}
    users: list[AdminSpendUser] = []
    # every known user appears (zero days included), plus a system row
    # when system spend exists in the window
    for uid in sorted(set(per_user) | set(rows_by_id),
                      key=lambda u: (u is None, u)):
        day_rows = per_user.get(
            uid, [spend_mod.zero_day(d)
                  for d in spend_mod.window_days(days)])
        row = rows_by_id.get(uid) if uid is not None else None
        users.append(AdminSpendUser(
            user_id=uid,
            email=row["email"] if row else None,
            name=row["name"] if row else None,
            today_usd=await spend_mod.spent_today(db, user_id=uid)
            if uid is not None else
            await _system_today(db),
            cap_usd=await general.user_cap(db, uid)
            if uid is not None else None,
            investigation_cap_usd=await investigation.user_cap(db, uid)
            if uid is not None else None,
            days=[SpendDay(**d) for d in day_rows],
        ))
    return AdminSpend(
        global_cap_usd=effective["global_daily_budget_usd"],
        global_today_usd=await spend_mod.spent_today(db),
        days=[SpendDay(**d)
              for d in await spend_mod.daily_breakdown(db, days)],
        users=users,
    )


async def _system_today(conn: psycopg.AsyncConnection) -> float:
    cur = await conn.execute(
        "SELECT COALESCE(SUM(cost_estimate), 0) AS total FROM llm_call"
        " WHERE (created_at AT TIME ZONE 'utc')::date = %s"
        " AND user_id IS NULL", (spend_mod.today_utc(),))
    return float((await cur.fetchone())["total"])
