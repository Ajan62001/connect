"""Beat: advisory-lock leader election (exactly one ticker; takeover on
leader death), due-source poll scheduling with index-backed dedup, the
nightly beat_run guard, and the brief pre-gen fan-out (active users only —
stubbed-by-data until auth writes last_login_at)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest
from dbutil import q1, qall, qv

from connect.storage import sources as source_dao
from connect.storage.pg import utc_now
from connect.workers import beat
from connect.workers.queue import PgJobQueue

pytestmark = pytest.mark.usefixtures("pg_database")


def make_services(settings, pool, *, adapters=("rss",)) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings,
        pool=pool,
        poller=SimpleNamespace(adapters={t: object() for t in adapters}),
        jobs=PgJobQueue(pool),
    )


async def insert_rss_source(conn, name="Beat Feed") -> int:
    source = await source_dao.insert(
        conn, name=name, type_="rss",
        config={"feed_url": "https://example.org/feed.xml"},
        credibility_tier=2)
    return source.id


# --- leader election -----------------------------------------------------------------


async def test_advisory_lock_is_single_holder(settings):
    dsn = settings.database_url
    a = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    b = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        got_a = await (await a.execute(
            "SELECT pg_try_advisory_lock(%s)", (beat.LOCK_BEAT,))).fetchone()
        got_b = await (await b.execute(
            "SELECT pg_try_advisory_lock(%s)", (beat.LOCK_BEAT,))).fetchone()
        assert got_a[0] is True and got_b[0] is False
        await a.close()  # leader dies -> PG releases the lock
        got_b2 = await (await b.execute(
            "SELECT pg_try_advisory_lock(%s)", (beat.LOCK_BEAT,))).fetchone()
        assert got_b2[0] is True
    finally:
        for conn in (a, b):
            if not conn.closed:
                await conn.close()


async def test_candidate_defers_then_takes_over(settings, pool,
                                                monkeypatch):
    """While another session holds LOCK_BEAT the candidate never ticks;
    when the holder dies the candidate becomes leader and ticks."""
    ticks: list[float] = []

    async def fake_tick(_services):
        ticks.append(asyncio.get_event_loop().time())

    monkeypatch.setattr(beat, "tick", fake_tick)
    holder = await psycopg.AsyncConnection.connect(
        settings.database_url, autocommit=True)
    row = await (await holder.execute(
        "SELECT pg_try_advisory_lock(%s)", (beat.LOCK_BEAT,))).fetchone()
    assert row[0] is True

    services = make_services(settings, pool)
    stop = asyncio.Event()
    task = asyncio.create_task(beat.beat_candidate(
        services, stop, tick_s=0.05, retry_s=0.05))
    try:
        await asyncio.sleep(0.4)
        assert ticks == []  # never leader while the lock is held
        await holder.close()
        deadline = asyncio.get_event_loop().time() + 5.0
        while not ticks and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert ticks, "candidate did not take over after the leader died"
    finally:
        stop.set()
        await task
        if not holder.closed:
            await holder.close()


# --- tick: due sources -> poll jobs ---------------------------------------------------


async def test_tick_enqueues_due_polls_once(db, pool, settings):
    source_id = await insert_rss_source(db)
    services = make_services(settings, pool)
    # nightly tasks must not fire in this test: pin the hour ahead
    hour = datetime.now(timezone.utc).hour
    services.settings = settings.model_copy(
        update={"poller_enabled": True,
                "nightly_sweep_utc_hour": (hour + 2) % 24})
    counts = await beat.tick(services)
    if datetime.now(timezone.utc).hour >= 22:  # wraparound day edge
        counts.pop("nightly", None)
    assert counts["polls"] == 1
    jobs = await qall(db, "SELECT * FROM job WHERE kind = 'poll_source'")
    assert len(jobs) == 1
    assert jobs[0]["payload"] == {"source_id": source_id}
    assert jobs[0]["priority"] == 90

    # second tick: the live job dedups; nothing new
    counts = await beat.tick(services)
    assert await qv(db, "SELECT COUNT(*) FROM job"
                        " WHERE kind = 'poll_source'") == 1

    # a freshly-polled source is no longer due
    await source_dao.set_poll_result(db, source_id, "ok: 0 new")
    await db.execute("UPDATE job SET status = 'done' WHERE kind ="
                     " 'poll_source'")
    counts = await beat.tick(services)
    assert counts["polls"] == 0


async def test_tick_respects_poller_disabled(db, pool, settings):
    await insert_rss_source(db)
    services = make_services(settings, pool)  # poller_enabled=False fixture
    hour = datetime.now(timezone.utc).hour
    services.settings = settings.model_copy(
        update={"nightly_sweep_utc_hour": (hour + 2) % 24})
    counts = await beat.tick(services)
    assert counts["polls"] == 0
    assert await qv(db, "SELECT COUNT(*) FROM job") == 0


# --- tick: orphan sweep ----------------------------------------------------------------


async def test_tick_runs_orphan_sweep(db, pool, settings):
    from connect.storage import jobs as job_dao

    job_id = await job_dao.create(db, "analysis", {})
    await job_dao.claim_one(db, job_id, "w-dead")
    await db.execute("UPDATE job SET heartbeat_at = now() - interval"
                     " '10 minutes' WHERE id = %s", (job_id,))
    services = make_services(settings, pool)
    hour = datetime.now(timezone.utc).hour
    services.settings = settings.model_copy(
        update={"nightly_sweep_utc_hour": (hour + 2) % 24})
    counts = await beat.tick(services)
    assert counts["orphan_failed"] == 1  # max_attempts=1: orphaned -> failed
    assert await qv(db, "SELECT status FROM job WHERE id = %s",
                    job_id) == "failed"


# --- nightly guard + brief pre-gen ------------------------------------------------------


async def test_nightly_due_fires_once_per_day(db):
    hour = datetime.now(timezone.utc).hour
    assert await beat._nightly_due(db, "enrich_t1_batch", hour) is True
    assert await beat._nightly_due(db, "enrich_t1_batch", hour) is False
    row = await q1(db, "SELECT last_run_at FROM beat_run WHERE task = %s",
                   "enrich_t1_batch")
    assert row is not None

    # an hour still in the future today never fires
    if hour < 23:
        assert await beat._nightly_due(db, "brief_pregen",
                                       hour + 1) is False

    # yesterday's run does not block today's threshold
    await db.execute(
        "UPDATE beat_run SET last_run_at = %s::timestamptz"
        " - interval '1 day' WHERE task = %s",
        (utc_now(), "enrich_t1_batch"))
    assert await beat._nightly_due(db, "enrich_t1_batch", hour) is True


async def test_nightly_tick_enqueues_batch_and_briefs(db, pool, settings):
    # two users: one active inside the 7-day window, one stale, one disabled
    await db.execute(
        "INSERT INTO app_user (email, created_at, last_login_at) VALUES"
        " ('active@example.com', %s, now() - interval '1 day'),"
        " ('stale@example.com', %s, now() - interval '30 days')",
        (utc_now(), utc_now()))
    await db.execute(
        "INSERT INTO app_user (email, created_at, last_login_at, disabled)"
        " VALUES ('off@example.com', %s, now(), true)", (utc_now(),))
    active_id = await qv(db, "SELECT id FROM app_user WHERE email ="
                             " 'active@example.com'")
    services = make_services(settings, pool)
    services.settings = settings.model_copy(
        update={"nightly_sweep_utc_hour":
                datetime.now(timezone.utc).hour})
    counts = await beat.tick(services)
    assert counts["nightly"] == 1 and counts["briefs"] == 1
    batch = await qall(db, "SELECT * FROM job"
                           " WHERE kind = 'enrich_t1_batch'")
    assert len(batch) == 1 and batch[0]["priority"] == 50
    briefs = await qall(db, "SELECT * FROM job"
                            " WHERE kind = 'brief_generate'")
    assert len(briefs) == 1
    assert briefs[0]["payload"] == {"user_id": active_id}
    assert briefs[0]["priority"] == 60

    # restart safety: a second tick the same day double-fires nothing
    counts = await beat.tick(services)
    assert counts["nightly"] == 0 and counts["briefs"] == 0
