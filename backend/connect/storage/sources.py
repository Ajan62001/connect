"""Source aggregate DAO — raw SQL only; returns domain contracts."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import psycopg

from connect.domain.models import Source
from connect.storage.pg import Jsonb, utc_now

_SELECT = """
SELECT s.*, (SELECT COUNT(*) FROM document d WHERE d.source_id = s.id) AS doc_count
FROM source s
"""


def _to_model(row: Mapping[str, Any]) -> Source:
    return Source(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        config=row["config"] or {},
        credibility_tier=row["credibility_tier"],
        enabled=bool(row["enabled"]),
        notes=row["notes"],
        t1_exempt=bool(row["t1_exempt"]),
        created_at=row["created_at"],
        last_polled_at=row["last_polled_at"],
        last_poll_status=row["last_poll_status"],
        reliability_score=row.get("reliability_score"),
        doc_count=row.get("doc_count", 0),
    )


async def insert(conn: psycopg.AsyncConnection, *, name: str, type_: str,
                 config: dict[str, Any], credibility_tier: int,
                 notes: str | None = None, enabled: bool = True,
                 t1_exempt: bool = False) -> Source:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO source (name, type, config, credibility_tier,"
            " enabled, t1_exempt, notes, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (name, type_, Jsonb(config), credibility_tier,
             bool(enabled), bool(t1_exempt), notes, utc_now()))
        source_id = (await cur.fetchone())["id"]
    return await get(conn, source_id)  # type: ignore[return-value]


async def get(conn: psycopg.AsyncConnection,
              source_id: int) -> Source | None:
    cur = await conn.execute(_SELECT + " WHERE s.id = %s", (source_id,))
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def get_by_name(conn: psycopg.AsyncConnection,
                      name: str) -> Source | None:
    cur = await conn.execute(_SELECT + " WHERE s.name = %s", (name,))
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def list_all(conn: psycopg.AsyncConnection) -> list[Source]:
    cur = await conn.execute(_SELECT + " ORDER BY s.id")
    return [_to_model(r) for r in await cur.fetchall()]


async def list_pollable(conn: psycopg.AsyncConnection,
                        types: Sequence[str] = ("rss",)) -> list[Source]:
    """Enabled sources of the given types (the poller passes its adapter
    keys — every discovery-capable type)."""
    if not types:
        return []
    cur = await conn.execute(
        _SELECT + " WHERE s.enabled AND s.type = ANY(%s) ORDER BY s.id",
        (list(types),))
    return [_to_model(r) for r in await cur.fetchall()]


_PATCHABLE = {
    "name": "name", "config": "config", "credibility_tier": "credibility_tier",
    "notes": "notes", "enabled": "enabled", "t1_exempt": "t1_exempt",
}


async def update(conn: psycopg.AsyncConnection, source_id: int,
                 fields: dict[str, Any]) -> Source | None:
    sets, params = [], []
    for key, col in _PATCHABLE.items():
        if key not in fields:
            continue
        value = fields[key]
        if key == "config":
            value = Jsonb(value)
        elif key in ("enabled", "t1_exempt"):
            value = bool(value)
        sets.append(f"{col} = %s")
        params.append(value)
    if sets:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE source SET {', '.join(sets)} WHERE id = %s",
                (*params, source_id))
    return await get(conn, source_id)


async def delete(conn: psycopg.AsyncConnection, source_id: int) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM source WHERE id = %s", (source_id,))
    return cur.rowcount > 0


async def set_poll_result(conn: psycopg.AsyncConnection, source_id: int,
                          status: str) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE source SET last_polled_at = %s, last_poll_status = %s"
            " WHERE id = %s",
            (utc_now(), status, source_id))


async def docs_today(conn: psycopg.AsyncConnection, source_id: int) -> int:
    """Documents ingested for this source since UTC midnight (per-day cap)."""
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM document WHERE source_id = %s"
        " AND fetched_at >= date_trunc('day', now() AT TIME ZONE 'utc')"
        "     AT TIME ZONE 'utc'",
        (source_id,))
    return int((await cur.fetchone())["n"])


async def bump_stats(conn: psycopg.AsyncConnection, source_id: int, *,
                     items: int = 0, dups: int = 0) -> None:
    """Upsert today's source_stats counters."""
    day = utc_now()[:10]
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO source_stats (source_id, day, items, dups)"
            " VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (source_id, day) DO UPDATE SET"
            " items = source_stats.items + EXCLUDED.items,"
            " dups = source_stats.dups + EXCLUDED.dups",
            (source_id, day, items, dups))
