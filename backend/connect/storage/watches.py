"""Watch aggregate DAO — CRUD + watch_hit rows + the badges query.

v0.2 Phase C: watches are PERSONAL (watch.user_id NOT NULL). Every CRUD
path scopes on the owning user (another user's watch reads as absent ->
404 at the router); ``list_active(conn)`` without a user stays the global
T0 matching view (every user's unmuted watches — match_document applies
the private-doc owner gate itself).
"""

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
        user_id=row["user_id"],
        workspace_id=row.get("workspace_id"),
    )


async def insert(conn: psycopg.AsyncConnection, *, user_id: int, kind: str,
                 label: str, query_fts: str | None = None,
                 entity_id: int | None = None,
                 promote: bool = True, muted: bool = False,
                 workspace_id: int | None = None) -> Watch:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO watch (user_id, kind, label, query_fts, entity_id,"
            " promote, muted, created_at, workspace_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (user_id, kind, label, query_fts, entity_id, bool(promote),
             bool(muted), utc_now(), workspace_id))
        watch_id = (await cur.fetchone())["id"]
    return await get(conn, watch_id)  # type: ignore[return-value]


async def get(conn: psycopg.AsyncConnection, watch_id: int, *,
              user_id: int | None = None) -> Watch | None:
    sql, params = "SELECT * FROM watch WHERE id = %s", [watch_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def list_all(conn: psycopg.AsyncConnection, user_id: int, *,
                   workspace_id: int | None = None) -> list[Watch]:
    sql = "SELECT * FROM watch WHERE user_id = %s"
    params: list[Any] = [user_id]
    if workspace_id is not None:
        sql += " AND workspace_id = %s"
        params.append(workspace_id)
    cur = await conn.execute(sql + " ORDER BY id", params)
    return [_to_model(r) for r in await cur.fetchall()]


async def list_active(conn: psycopg.AsyncConnection,
                      user_id: int | None = None) -> list[Watch]:
    """Unmuted watches — all users' (the T0 matching view) or one user's
    (the per-user brief sections)."""
    sql, params = "SELECT * FROM watch WHERE NOT muted", []
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    cur = await conn.execute(sql + " ORDER BY id", params)
    return [_to_model(r) for r in await cur.fetchall()]


_PATCHABLE = ("label", "query_fts", "entity_id", "promote", "muted")


async def update(conn: psycopg.AsyncConnection, watch_id: int,
                 fields: dict[str, Any], *,
                 user_id: int | None = None) -> Watch | None:
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
        sql = f"UPDATE watch SET {', '.join(sets)} WHERE id = %s"
        params.append(watch_id)
        if user_id is not None:
            sql += " AND user_id = %s"
            params.append(user_id)
        async with conn.transaction():
            await conn.execute(sql, params)
    return await get(conn, watch_id, user_id=user_id)


async def delete(conn: psycopg.AsyncConnection, watch_id: int, *,
                 user_id: int | None = None) -> bool:
    sql, params = "DELETE FROM watch WHERE id = %s", [watch_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    async with conn.transaction():
        cur = await conn.execute(sql, params)
    return cur.rowcount > 0


async def mark_seen(conn: psycopg.AsyncConnection, watch_id: int, *,
                    user_id: int | None = None) -> Watch | None:
    sql = "UPDATE watch SET last_seen_at = %s WHERE id = %s"
    params: list[Any] = [utc_now(), watch_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    async with conn.transaction():
        cur = await conn.execute(sql, params)
    if cur.rowcount == 0:
        return None
    return await get(conn, watch_id, user_id=user_id)


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


async def badges(conn: psycopg.AsyncConnection,
                 user_id: int) -> dict[int, int]:
    """Unread watch_hit counts past each of the USER's watches' cursors."""
    cur = await conn.execute(
        "SELECT w.id, COUNT(h.object_id) AS unread"
        " FROM watch w LEFT JOIN watch_hit h"
        "   ON h.watch_id = w.id"
        "   AND h.created_at > COALESCE(w.last_seen_at, '1970-01-01')"
        " WHERE NOT w.muted AND w.user_id = %s GROUP BY w.id",
        (user_id,))
    return {int(r["id"]): int(r["unread"]) for r in await cur.fetchall()}
