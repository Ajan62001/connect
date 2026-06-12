"""user_session DAO — opaque server-side sessions (tenancy design §3).

Row mechanics only; expiry/touch POLICY lives in connect/auth/sessions.py.
Timestamps are ISO strings (storage/pg.py loaders), compared as text against
utc_now() — the codebase-wide convention.
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.storage.pg import utc_now


async def insert(conn: psycopg.AsyncConnection, *, session_id: str,
                 user_id: int, expires_at: str) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO user_session (id, user_id, created_at, expires_at,"
            " last_seen_at) VALUES (%s, %s, %s, %s, %s)",
            (session_id, user_id, utc_now(), expires_at, utc_now()))


async def get_with_user(conn: psycopg.AsyncConnection,
                        session_id: str) -> dict[str, Any] | None:
    """The session row joined with its app_user row (one indexed lookup per
    request — the whole point of the PG-sessions pick)."""
    cur = await conn.execute(
        "SELECT s.id AS session_id, s.created_at AS session_created_at,"
        " s.expires_at, s.last_seen_at, u.*"
        " FROM user_session s JOIN app_user u ON u.id = s.user_id"
        " WHERE s.id = %s",
        (session_id,))
    return await cur.fetchone()


async def touch(conn: psycopg.AsyncConnection, session_id: str,
                expires_at: str) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE user_session SET last_seen_at = %s, expires_at = %s"
            " WHERE id = %s",
            (utc_now(), expires_at, session_id))


async def delete(conn: psycopg.AsyncConnection, session_id: str) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM user_session WHERE id = %s", (session_id,))
    return cur.rowcount > 0


async def delete_for_user(conn: psycopg.AsyncConnection,
                          user_id: int) -> int:
    """Logout-all (admin disables a user => instant revocation)."""
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM user_session WHERE user_id = %s", (user_id,))
    return cur.rowcount


async def gc_expired(conn: psycopg.AsyncConnection) -> int:
    """Drop expired rows (called from a periodic job; idx_session_expires)."""
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM user_session WHERE expires_at <= %s", (utc_now(),))
    return cur.rowcount
