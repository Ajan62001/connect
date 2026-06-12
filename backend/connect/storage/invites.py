"""invite DAO — the email allowlist gate (tenancy design §3; admin-only
invites per the locked decision). Emails are stored lowercased."""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import Invite
from connect.storage.pg import utc_now


def to_model(row: Mapping[str, Any]) -> Invite:
    return Invite(
        email=row["email"],
        invited_by=row["invited_by"],
        note=row["note"],
        created_at=row["created_at"],
    )


async def get(conn: psycopg.AsyncConnection,
              email: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM invite WHERE email = lower(%s)", (email,))
    return await cur.fetchone()


async def insert(conn: psycopg.AsyncConnection, *, email: str,
                 invited_by: int | None,
                 note: str | None = None) -> dict[str, Any] | None:
    """Add to the allowlist; None when the email is already invited."""
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO invite (email, invited_by, note, created_at)"
            " VALUES (lower(%s), %s, %s, %s)"
            " ON CONFLICT (email) DO NOTHING RETURNING *",
            (email, invited_by, note, utc_now()))
        return await cur.fetchone()


async def delete(conn: psycopg.AsyncConnection, email: str) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM invite WHERE email = lower(%s)", (email,))
    return cur.rowcount > 0


async def list_all(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM invite ORDER BY created_at")
    return await cur.fetchall()
