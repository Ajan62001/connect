"""Tiny async query helpers for tests — keep ported call sites one-liners.

dict_row preserves SELECT column order, so positional access (the sqlite3
``row[0]`` habit) maps to ``list(row.values())[i]``.
"""

from __future__ import annotations

from typing import Any


async def q1(conn, sql: str, *params) -> dict | None:
    """First row (dict) or None."""
    cur = await conn.execute(sql, params or None)
    return await cur.fetchone()


async def qall(conn, sql: str, *params) -> list[dict]:
    cur = await conn.execute(sql, params or None)
    return await cur.fetchall()


async def qv(conn, sql: str, *params) -> Any:
    """First column of the first row (sqlite3 ``fetchone()[0]``)."""
    row = await q1(conn, sql, *params)
    assert row is not None, f"no row for {sql!r}"
    return next(iter(row.values()))


async def qvals(conn, sql: str, *params) -> list[Any]:
    """First column of every row."""
    return [next(iter(r.values())) for r in await qall(conn, sql, *params)]


async def qtuples(conn, sql: str, *params) -> list[tuple]:
    """Rows as positional tuples (sqlite3 ``tuple(row)``)."""
    return [tuple(r.values()) for r in await qall(conn, sql, *params)]


async def ex(conn, sql: str, *params) -> int:
    """Execute one statement; returns rowcount (autocommit)."""
    cur = await conn.execute(sql, params or None)
    return cur.rowcount
