"""Source aggregate DAO — raw SQL only; returns domain contracts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from connect.domain.models import Source
from connect.storage.db import utc_now

_SELECT = """
SELECT s.*, (SELECT COUNT(*) FROM document d WHERE d.source_id = s.id) AS doc_count
FROM source s
"""


def _to_model(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        config=json.loads(row["config"] or "{}"),
        credibility_tier=row["credibility_tier"],
        enabled=bool(row["enabled"]),
        notes=row["notes"],
        t1_exempt=bool(row["t1_exempt"]),
        created_at=row["created_at"],
        last_polled_at=row["last_polled_at"],
        last_poll_status=row["last_poll_status"],
        doc_count=row["doc_count"] if "doc_count" in row.keys() else 0,
    )


def insert(conn: sqlite3.Connection, *, name: str, type_: str,
           config: dict[str, Any], credibility_tier: int,
           notes: str | None = None, enabled: bool = True,
           t1_exempt: bool = False) -> Source:
    with conn:
        cur = conn.execute(
            "INSERT INTO source (name, type, config, credibility_tier, enabled,"
            " t1_exempt, notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, type_, json.dumps(config), credibility_tier,
             int(enabled), int(t1_exempt), notes, utc_now()))
    return get(conn, cur.lastrowid)  # type: ignore[arg-type]


def get(conn: sqlite3.Connection, source_id: int) -> Source | None:
    row = conn.execute(_SELECT + " WHERE s.id = ?", (source_id,)).fetchone()
    return _to_model(row) if row else None


def get_by_name(conn: sqlite3.Connection, name: str) -> Source | None:
    row = conn.execute(_SELECT + " WHERE s.name = ?", (name,)).fetchone()
    return _to_model(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Source]:
    rows = conn.execute(_SELECT + " ORDER BY s.id").fetchall()
    return [_to_model(r) for r in rows]


def list_pollable(conn: sqlite3.Connection,
                  types: Sequence[str] = ("rss",)) -> list[Source]:
    """Enabled sources of the given types (the poller passes its adapter
    keys — every discovery-capable type)."""
    if not types:
        return []
    placeholders = ", ".join("?" for _ in types)
    rows = conn.execute(
        _SELECT + f" WHERE s.enabled = 1 AND s.type IN ({placeholders})"
        " ORDER BY s.id", tuple(types)).fetchall()
    return [_to_model(r) for r in rows]


_PATCHABLE = {
    "name": "name", "config": "config", "credibility_tier": "credibility_tier",
    "notes": "notes", "enabled": "enabled", "t1_exempt": "t1_exempt",
}


def update(conn: sqlite3.Connection, source_id: int,
           fields: dict[str, Any]) -> Source | None:
    sets, params = [], []
    for key, col in _PATCHABLE.items():
        if key not in fields:
            continue
        value = fields[key]
        if key == "config":
            value = json.dumps(value)
        elif key in ("enabled", "t1_exempt"):
            value = int(value)
        sets.append(f"{col} = ?")
        params.append(value)
    if sets:
        with conn:
            conn.execute(
                f"UPDATE source SET {', '.join(sets)} WHERE id = ?",
                (*params, source_id))
    return get(conn, source_id)


def delete(conn: sqlite3.Connection, source_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM source WHERE id = ?", (source_id,))
    return cur.rowcount > 0


def set_poll_result(conn: sqlite3.Connection, source_id: int,
                    status: str) -> None:
    with conn:
        conn.execute(
            "UPDATE source SET last_polled_at = ?, last_poll_status = ? WHERE id = ?",
            (utc_now(), status, source_id))


def docs_today(conn: sqlite3.Connection, source_id: int) -> int:
    """Documents ingested for this source since UTC midnight (per-day cap)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM document WHERE source_id = ?"
        " AND fetched_at >= strftime('%Y-%m-%dT00:00:00', 'now')",
        (source_id,)).fetchone()
    return int(row[0])


def bump_stats(conn: sqlite3.Connection, source_id: int, *,
               items: int = 0, dups: int = 0) -> None:
    """Upsert today's source_stats counters."""
    day = utc_now()[:10]
    with conn:
        conn.execute(
            "INSERT INTO source_stats (source_id, day, items, dups)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(source_id, day) DO UPDATE SET"
            " items = items + excluded.items, dups = dups + excluded.dups",
            (source_id, day, items, dups))
