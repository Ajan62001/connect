"""Watch aggregate DAO — CRUD + watch_hit rows + the badges query."""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import Watch
from connect.storage.pg import utc_now


def _to_model(row: Mapping[str, Any]) -> Watch:
    return Watch(
        id=row["id"],
        kind=row["kind"],
        label=row["label"],
        query_fts=row["query_fts"],
        entity_id=row["entity_id"],
        promote=bool(row["promote"]),
        muted=bool(row["muted"]),
        last_seen_at=row["last_seen_at"],
        created_at=row["created_at"],
    )


async def insert(conn: psycopg.AsyncConnection, *, kind: str, label: str,
                 query_fts: str | None = None, entity_id: int | None = None,
                 promote: bool = True, muted: bool = False) -> Watch:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO watch (kind, label, query_fts, entity_id, promote,"
            " muted, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (kind, label, query_fts, entity_id, bool(promote), bool(muted),
             utc_now()))
        watch_id = (await cur.fetchone())["id"]
    return await get(conn, watch_id)  # type: ignore[return-value]


async def get(conn: psycopg.AsyncConnection, watch_id: int) -> Watch | None:
    cur = await conn.execute(
        "SELECT * FROM watch WHERE id = %s", (watch_id,))
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def list_all(conn: psycopg.AsyncConnection) -> list[Watch]:
    cur = await conn.execute("SELECT * FROM watch ORDER BY id")
    return [_to_model(r) for r in await cur.fetchall()]


async def list_active(conn: psycopg.AsyncConnection) -> list[Watch]:
    cur = await conn.execute(
        "SELECT * FROM watch WHERE NOT muted ORDER BY id")
    return [_to_model(r) for r in await cur.fetchall()]


_PATCHABLE = ("label", "query_fts", "entity_id", "promote", "muted")


async def update(conn: psycopg.AsyncConnection, watch_id: int,
                 fields: dict[str, Any]) -> Watch | None:
    sets, params = [], []
    for col in _PATCHABLE:
        if col not in fields:
            continue
        value = fields[col]
        if col in ("promote", "muted"):
            value = bool(value)
        sets.append(f"{col} = %s")
        params.append(value)
    if sets:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE watch SET {', '.join(sets)} WHERE id = %s",
                (*params, watch_id))
    return await get(conn, watch_id)


async def delete(conn: psycopg.AsyncConnection, watch_id: int) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM watch WHERE id = %s", (watch_id,))
    return cur.rowcount > 0


async def mark_seen(conn: psycopg.AsyncConnection,
                    watch_id: int) -> Watch | None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE watch SET last_seen_at = %s WHERE id = %s",
            (utc_now(), watch_id))
    return await get(conn, watch_id)


async def insert_hit(conn: psycopg.AsyncConnection, watch_id: int,
                     object_type: str, object_id: int) -> bool:
    """Record a watch hit; idempotent (PK = watch, object). True if new."""
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO watch_hit (watch_id, object_type,"
            " object_id, created_at) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (watch_id, object_type, object_id) DO NOTHING",
            (watch_id, object_type, object_id, utc_now()))
    return cur.rowcount > 0


async def badges(conn: psycopg.AsyncConnection) -> dict[int, int]:
    """Unread watch_hit counts past each watch's read cursor."""
    cur = await conn.execute(
        "SELECT w.id, COUNT(h.object_id) AS unread"
        " FROM watch w LEFT JOIN watch_hit h"
        "   ON h.watch_id = w.id"
        "   AND h.created_at > COALESCE(w.last_seen_at, '1970-01-01')"
        " WHERE NOT w.muted GROUP BY w.id")
    return {int(r["id"]): int(r["unread"]) for r in await cur.fetchall()}
