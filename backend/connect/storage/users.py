"""app_user DAO — identity rows for auth (tenancy design §2/§3).

Pure row access; the login/gate POLICY (invite checks, first-user-admin,
account linking) lives in connect/auth/oauth.py; the admin management
POLICY (self-lockout guards, session revocation on disable) lives in
api/routers/admin.py.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import AdminUser, CurrentUser
from connect.storage.pg import utc_now

# the only columns PATCH /api/admin/users/{id} may touch
ADMIN_MUTABLE_COLUMNS = ("role", "disabled", "daily_budget_usd",
                         "investigation_daily_budget_usd")


def to_model(row: Mapping[str, Any]) -> CurrentUser:
    return CurrentUser(
        id=row["id"],
        email=row["email"],
        name=row["name"],
        avatar_url=row["avatar_url"],
        role=row["role"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def to_admin_model(row: Mapping[str, Any]) -> AdminUser:
    return AdminUser(
        id=row["id"],
        email=row["email"],
        name=row["name"],
        avatar_url=row["avatar_url"],
        role=row["role"],
        disabled=row["disabled"],
        daily_budget_usd=row["daily_budget_usd"],
        investigation_daily_budget_usd=row[
            "investigation_daily_budget_usd"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


async def get(conn: psycopg.AsyncConnection,
              user_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM app_user WHERE id = %s", (user_id,))
    return await cur.fetchone()


async def list_all(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM app_user ORDER BY id")
    return await cur.fetchall()


async def admin_update(conn: psycopg.AsyncConnection, user_id: int,
                       changes: Mapping[str, Any]) -> dict[str, Any] | None:
    """Apply admin-mutable column changes (whitelisted — a non-mutable key
    is a programming error); returns the updated row or None (unknown
    id). An explicit None value clears a budget override to NULL."""
    assert changes, "admin_update needs at least one change"
    for col in changes:
        assert col in ADMIN_MUTABLE_COLUMNS, col
    sets = ", ".join(f"{col} = %s" for col in changes)
    params: list[Any] = list(changes.values()) + [user_id]
    async with conn.transaction():
        cur = await conn.execute(
            f"UPDATE app_user SET {sets} WHERE id = %s RETURNING *",
            params)
        return await cur.fetchone()


async def get_by_google_sub(conn: psycopg.AsyncConnection,
                            sub: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM app_user WHERE google_sub = %s", (sub,))
    return await cur.fetchone()


async def get_by_email(conn: psycopg.AsyncConnection,
                       email: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM app_user WHERE lower(email) = lower(%s)", (email,))
    return await cur.fetchone()


async def count(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("SELECT count(*) AS n FROM app_user")
    return int((await cur.fetchone())["n"])


async def insert(conn: psycopg.AsyncConnection, *, email: str,
                 google_sub: str | None = None, name: str | None = None,
                 avatar_url: str | None = None,
                 role: str = "member") -> dict[str, Any]:
    """Create a user; caller owns the gate decision AND the surrounding
    transaction/lock (first-user-admin must be race-safe)."""
    cur = await conn.execute(
        "INSERT INTO app_user (google_sub, email, name, avatar_url, role,"
        " created_at, last_login_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (google_sub, email.lower(), name, avatar_url, role,
         utc_now(), utc_now()))
    return await cur.fetchone()  # type: ignore[return-value]


async def record_login(conn: psycopg.AsyncConnection, user_id: int, *,
                       google_sub: str | None = None,
                       name: str | None = None,
                       avatar_url: str | None = None) -> dict[str, Any]:
    """Stamp last_login_at and refresh profile fields from the ID token.

    google_sub fills only when currently NULL (account linking: the ETL'd /
    pre-created row activates at first Google login); name/avatar update
    when Google supplies them.
    """
    cur = await conn.execute(
        "UPDATE app_user SET"
        " last_login_at = %s,"
        " google_sub = COALESCE(google_sub, %s),"
        " name = COALESCE(%s, name),"
        " avatar_url = COALESCE(%s, avatar_url)"
        " WHERE id = %s RETURNING *",
        (utc_now(), google_sub, name, avatar_url, user_id))
    return await cur.fetchone()  # type: ignore[return-value]
