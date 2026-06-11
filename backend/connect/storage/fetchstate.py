"""Shared fetch politeness state (runtime design §4) — fetch_domain slot
reservations + the robots.txt cache.

The reservation is ONE atomic upsert: every process asks Postgres for the
next free slot for a domain and sleeps locally until it; exact
1-request-per-interval-per-domain politeness across any number of
api/worker processes, no daemon. GREATEST keeps idle domains from
accumulating backlog. The statement must commit immediately (autocommit
connection, never inside a larger transaction) or reservations serialize.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg


async def reserve_slot(conn: psycopg.AsyncConnection, domain: str,
                       interval_s: float) -> float:
    """Reserve the domain's next fetch slot; returns seconds to sleep
    before fetching (0.0 = go now)."""
    cur = await conn.execute(
        """INSERT INTO fetch_domain (domain, next_at)
           VALUES (%(d)s, now() + make_interval(secs => %(iv)s))
           ON CONFLICT (domain) DO UPDATE
             SET next_at = GREATEST(fetch_domain.next_at, now())
                           + make_interval(secs => %(iv)s)
           RETURNING greatest(
               0.0, extract(epoch FROM (next_at
                            - make_interval(secs => %(iv)s)
                            - now())))::float8 AS wait_s""",
        {"d": domain, "iv": interval_s})
    row = await cur.fetchone()
    return float(row["wait_s"])


async def get_robots(conn: psycopg.AsyncConnection, origin: str, *,
                     ttl_s: float = 3600.0) -> Mapping[str, Any] | None:
    """The cached robots.txt row for an origin, or None when absent.
    ``fresh`` is computed server-side against the TTL; ``body`` is NULL
    when robots.txt was unreachable (v0.1 semantics: allow)."""
    cur = await conn.execute(
        "SELECT body, fetched_at,"
        " (fetched_at >= now() - make_interval(secs => %s)) AS fresh"
        " FROM robots_cache WHERE origin = %s",
        (ttl_s, origin))
    return await cur.fetchone()


async def upsert_robots(conn: psycopg.AsyncConnection, origin: str,
                        body: str | None) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO robots_cache (origin, body, fetched_at)"
            " VALUES (%s, %s, now())"
            " ON CONFLICT (origin) DO UPDATE"
            " SET body = EXCLUDED.body, fetched_at = EXCLUDED.fetched_at",
            (origin, body))
