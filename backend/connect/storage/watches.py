"""Watch aggregate DAO — CRUD + watch_hit rows + the badges query."""

from __future__ import annotations

import sqlite3
from typing import Any

from connect.domain.models import Watch
from connect.storage.db import utc_now


def _to_model(row: sqlite3.Row) -> Watch:
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


def insert(conn: sqlite3.Connection, *, kind: str, label: str,
           query_fts: str | None = None, entity_id: int | None = None,
           promote: bool = True, muted: bool = False) -> Watch:
    with conn:
        cur = conn.execute(
            "INSERT INTO watch (kind, label, query_fts, entity_id, promote,"
            " muted, created_at) VALUES (?,?,?,?,?,?,?)",
            (kind, label, query_fts, entity_id, int(promote), int(muted),
             utc_now()))
    return get(conn, cur.lastrowid)  # type: ignore[arg-type]


def get(conn: sqlite3.Connection, watch_id: int) -> Watch | None:
    row = conn.execute(
        "SELECT * FROM watch WHERE id = ?", (watch_id,)).fetchone()
    return _to_model(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Watch]:
    rows = conn.execute("SELECT * FROM watch ORDER BY id").fetchall()
    return [_to_model(r) for r in rows]


def list_active(conn: sqlite3.Connection) -> list[Watch]:
    rows = conn.execute(
        "SELECT * FROM watch WHERE muted = 0 ORDER BY id").fetchall()
    return [_to_model(r) for r in rows]


_PATCHABLE = ("label", "query_fts", "entity_id", "promote", "muted")


def update(conn: sqlite3.Connection, watch_id: int,
           fields: dict[str, Any]) -> Watch | None:
    sets, params = [], []
    for col in _PATCHABLE:
        if col not in fields:
            continue
        value = fields[col]
        if col in ("promote", "muted"):
            value = int(value)
        sets.append(f"{col} = ?")
        params.append(value)
    if sets:
        with conn:
            conn.execute(
                f"UPDATE watch SET {', '.join(sets)} WHERE id = ?",
                (*params, watch_id))
    return get(conn, watch_id)


def delete(conn: sqlite3.Connection, watch_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM watch WHERE id = ?", (watch_id,))
    return cur.rowcount > 0


def mark_seen(conn: sqlite3.Connection, watch_id: int) -> Watch | None:
    with conn:
        conn.execute(
            "UPDATE watch SET last_seen_at = ? WHERE id = ?",
            (utc_now(), watch_id))
    return get(conn, watch_id)


def insert_hit(conn: sqlite3.Connection, watch_id: int, object_type: str,
               object_id: int) -> bool:
    """Record a watch hit; idempotent (PK = watch, object). True if new."""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO watch_hit (watch_id, object_type,"
            " object_id, created_at) VALUES (?,?,?,?)",
            (watch_id, object_type, object_id, utc_now()))
    return cur.rowcount > 0


def badges(conn: sqlite3.Connection) -> dict[int, int]:
    """Unread watch_hit counts past each watch's read cursor."""
    rows = conn.execute(
        "SELECT w.id, COUNT(h.object_id) AS unread"
        " FROM watch w LEFT JOIN watch_hit h"
        "   ON h.watch_id = w.id"
        "   AND h.created_at > COALESCE(w.last_seen_at, '1970-01-01')"
        " WHERE w.muted = 0 GROUP BY w.id").fetchall()
    return {int(r[0]): int(r[1]) for r in rows}
