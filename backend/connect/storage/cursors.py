"""view_cursor DAO — the generic per-surface read cursor.

POST /api/cursors upserts (surface, ref_id) -> last_seen_at=now; consumers
(entity DeltaBanner and friends) render "what changed since I last looked"
from created_at > cursor counts. Fully deterministic, zero LLM.
"""

from __future__ import annotations

import sqlite3

from connect.storage.db import utc_now


def upsert(conn: sqlite3.Connection, surface: str, ref_id: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO view_cursor (surface, ref_id, last_seen_at)"
            " VALUES (?,?,?)"
            " ON CONFLICT (surface, ref_id) DO UPDATE SET last_seen_at ="
            " excluded.last_seen_at",
            (surface, ref_id, utc_now()))


def get(conn: sqlite3.Connection, surface: str, ref_id: int) -> str | None:
    """The cursor's last_seen_at, or None when never visited."""
    row = conn.execute(
        "SELECT last_seen_at FROM view_cursor WHERE surface = ? AND ref_id = ?",
        (surface, ref_id)).fetchone()
    return row[0] if row else None
