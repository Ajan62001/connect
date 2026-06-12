"""view_cursor DAO — the generic PER-USER per-surface read cursor.

POST /api/cursors upserts (user_id, surface, ref_id) -> last_seen_at=now;
consumers (entity DeltaBanner and friends) render "what changed since I
last looked" from created_at > cursor counts. Fully deterministic, zero
LLM. v0.2 Phase C: the PK is (user_id, surface, ref_id) — cursors never
leak across users.
"""

from __future__ import annotations

import psycopg

from connect.storage.pg import utc_now


async def upsert(conn: psycopg.AsyncConnection, user_id: int, surface: str,
                 ref_id: int) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO view_cursor (user_id, surface, ref_id,"
            " last_seen_at) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (user_id, surface, ref_id) DO UPDATE SET"
            " last_seen_at = EXCLUDED.last_seen_at",
            (user_id, surface, ref_id, utc_now()))


async def get(conn: psycopg.AsyncConnection, user_id: int, surface: str,
              ref_id: int) -> str | None:
    """The cursor's last_seen_at, or None when never visited."""
    cur = await conn.execute(
        "SELECT last_seen_at FROM view_cursor"
        " WHERE user_id = %s AND surface = %s AND ref_id = %s",
        (user_id, surface, ref_id))
    row = await cur.fetchone()
    return row["last_seen_at"] if row else None
