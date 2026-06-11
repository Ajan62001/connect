"""view_cursor DAO — the generic per-surface read cursor.

POST /api/cursors upserts (surface, ref_id) -> last_seen_at=now; consumers
(entity DeltaBanner and friends) render "what changed since I last looked"
from created_at > cursor counts. Fully deterministic, zero LLM.
"""

from __future__ import annotations

import psycopg

from connect.storage.pg import utc_now


async def upsert(conn: psycopg.AsyncConnection, surface: str,
                 ref_id: int) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO view_cursor (surface, ref_id, last_seen_at)"
            " VALUES (%s,%s,%s)"
            " ON CONFLICT (surface, ref_id) DO UPDATE SET last_seen_at ="
            " EXCLUDED.last_seen_at",
            (surface, ref_id, utc_now()))


async def get(conn: psycopg.AsyncConnection, surface: str,
              ref_id: int) -> str | None:
    """The cursor's last_seen_at, or None when never visited."""
    cur = await conn.execute(
        "SELECT last_seen_at FROM view_cursor"
        " WHERE surface = %s AND ref_id = %s",
        (surface, ref_id))
    row = await cur.fetchone()
    return row["last_seen_at"] if row else None
