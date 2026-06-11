"""Shared politeness state (runtime design §4): fetch_domain slot
reservations space fetches per domain across connections/processes, idle
domains accumulate no backlog, and the robots_cache read-through keeps the
v0.1 semantics (NULL body = unreachable = allow) without re-fetching
robots.txt per process."""

from __future__ import annotations

import asyncio
import time

import pytest

from connect.ingestion.fetcher import Fetcher
from connect.storage import fetchstate

pytestmark = pytest.mark.usefixtures("pg_database")


# --- slot reservations ----------------------------------------------------------------


async def test_reserved_slots_space_out_per_domain(db, pool):
    # three reservations, two different sessions -> 0, ~iv, ~2*iv
    w1 = await fetchstate.reserve_slot(db, "example.org", 10.0)
    async with pool.connection() as other:
        w2 = await fetchstate.reserve_slot(other, "example.org", 10.0)
    w3 = await fetchstate.reserve_slot(db, "example.org", 10.0)
    assert w1 == 0.0
    assert 9.0 < w2 <= 10.0
    assert 19.0 < w3 <= 20.0


async def test_domains_are_independent(db):
    assert await fetchstate.reserve_slot(db, "a.example", 10.0) == 0.0
    assert await fetchstate.reserve_slot(db, "b.example", 10.0) == 0.0


async def test_idle_domain_accumulates_no_backlog(db):
    assert await fetchstate.reserve_slot(db, "idle.example", 0.05) == 0.0
    await asyncio.sleep(0.2)  # domain idle well past its interval
    # GREATEST(next_at, now()): the next slot is NOW, not 3 intervals ago
    wait = await fetchstate.reserve_slot(db, "idle.example", 0.05)
    assert wait == 0.0


async def test_fetcher_throttle_uses_shared_state(pool):
    fetcher = Fetcher(per_domain_interval=0.2)
    fetcher.bind_pool(pool)
    t0 = time.monotonic()
    await fetcher._throttle("slow.example")
    first = time.monotonic() - t0
    await fetcher._throttle("slow.example")
    elapsed = time.monotonic() - t0
    assert first < 0.15          # first slot is immediate
    assert elapsed >= 0.18       # second waited out the interval
    # and the state is visible to OTHER processes via the table
    async with pool.connection() as conn:
        wait = await fetchstate.reserve_slot(conn, "slow.example", 0.2)
    assert wait > 0.0


# --- robots cache -----------------------------------------------------------------------


async def test_robots_cache_roundtrip_and_ttl(db):
    assert await fetchstate.get_robots(db, "https://x.example") is None
    await fetchstate.upsert_robots(db, "https://x.example",
                                   "User-agent: *\nDisallow: /private")
    row = await fetchstate.get_robots(db, "https://x.example")
    assert row["fresh"] is True and "Disallow" in row["body"]
    # NULL body persists (unreachable robots -> allow)
    await fetchstate.upsert_robots(db, "https://x.example", None)
    row = await fetchstate.get_robots(db, "https://x.example")
    assert row["body"] is None and row["fresh"] is True
    # stale row reported as such (read-time TTL)
    await db.execute("UPDATE robots_cache SET fetched_at = now() -"
                     " interval '2 hours' WHERE origin = %s",
                     ("https://x.example",))
    row = await fetchstate.get_robots(db, "https://x.example")
    assert row["fresh"] is False


async def test_fetcher_robots_reads_shared_cache(db, pool):
    """A fresh shared row answers robots checks with NO network: disallow
    rules enforced, NULL body allows (v0.1 semantics)."""
    await fetchstate.upsert_robots(db, "https://blocked.example",
                                   "User-agent: *\nDisallow: /")
    await fetchstate.upsert_robots(db, "https://open.example", None)
    fetcher = Fetcher()
    fetcher.bind_pool(pool)
    assert await fetcher._robots_allowed(
        "https://blocked.example/page") is False
    assert await fetcher._robots_allowed(
        "https://open.example/page") is True
    # the in-process cache now answers without touching the pool
    fetcher._pool = None  # if it queried again, NULL pool would fall back
    assert await fetcher._robots_allowed(
        "https://blocked.example/other") is False
