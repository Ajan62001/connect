"""Beat — the scheduler coroutine every worker runs; exactly one is leader
(runtime design §4). No separate container: leadership is a Postgres
advisory lock held on a DEDICATED connection (session-scoped — a dead
worker's lock is released by Postgres, and a surviving candidate takes
over within ``retry_s``). Beat only ENQUEUES, never works:

| tick (60s)    | action                                                  |
|---------------|---------------------------------------------------------|
| orphan sweep  | requeue/fail 'running' jobs with stale heartbeats       |
| due sources   | one 'poll_source' job per due source (priority 90; the  |
|               | partial unique index dedups)                            |
| nightly batch | 'enrich_t1_batch' at CONNECT_NIGHTLY_SWEEP_UTC_HOUR,    |
|               | guarded by a beat_run row (restarts don't double-fire)  |
| brief pre-gen | 'brief_generate' per user active in the last 7 days     |
|               | (last_login_at, stamped on login), run_at staggered     |
|               | over an hour; inactive users stay on-demand.            |
| due content   | 'content_publish' per scheduled content_item whose      |
|               | scheduled_at has passed (the partial unique index       |
|               | dedups a re-enqueue while the publish job runs).        |
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg

from connect.ingestion.poller import is_due
from connect.knowledge import corrections
from connect.storage import content_items as content_item_dao
from connect.storage import jobs as job_dao
from connect.storage import sources as source_dao
from connect.storage.pg import utc_now

log = logging.getLogger(__name__)

# 64-bit advisory-lock key for beat leadership: ASCII "cnctbeat".
LOCK_BEAT = 0x636E637462656174

BRIEF_STAGGER_WINDOW_S = 3600.0


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def beat_candidate(services: Any, stop: asyncio.Event, *,
                         tick_s: float | None = None,
                         retry_s: float | None = None) -> None:
    """Run forever (until ``stop``): try to become the beat leader; the
    holder ticks, losers retry. Beat is a coroutine, not a job — it ticks
    even when every claim slot is saturated (design risk #7)."""
    settings = services.settings
    tick_s = settings.beat_tick_seconds if tick_s is None else tick_s
    retry_s = settings.beat_retry_seconds if retry_s is None else retry_s
    while not stop.is_set():
        try:
            conn = await psycopg.AsyncConnection.connect(
                settings.database_url, autocommit=True)
        except psycopg.Error as e:
            log.warning("beat: cannot reach DB (%s); retry in %.0fs",
                        e, retry_s)
            await _wait(stop, retry_s)
            continue
        try:
            cur = await conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (LOCK_BEAT,))
            got = bool((await cur.fetchone())[0])
            if not got:
                await _wait(stop, retry_s)
                continue
            log.info("beat: this worker is the leader")
            while not stop.is_set():
                try:
                    await tick(services)
                except Exception:  # noqa: BLE001 — the loop survives anything
                    log.exception("beat tick failed")
                await _wait(stop, tick_s)
                # the lock lives on THIS session — if it died, our
                # leadership is gone and another candidate may already
                # hold it; fall back to candidacy
                try:
                    await conn.execute("SELECT 1")
                except psycopg.Error:
                    log.warning("beat: leader session died; re-running"
                                " for election")
                    break
        finally:
            try:
                await conn.close()  # releases the advisory lock
            except Exception:  # noqa: BLE001
                pass


async def tick(services: Any) -> dict[str, int]:
    """One beat pass; returns counters (for tests/logs)."""
    settings = services.settings
    counts = {"requeued": 0, "orphan_failed": 0, "polls": 0,
              "nightly": 0, "briefs": 0, "publishes": 0,
              "corrections": 0, "rechecks": 0, "credibility": 0,
              "integrity_eval": 0}
    async with services.pool.connection() as conn:
        # 1. orphan sweep — replaces v0.1's startup reconcile_orphans
        requeued, failed = await job_dao.reclaim_stale(
            conn, stale_after_s=settings.job_stale_seconds)
        counts["requeued"], counts["orphan_failed"] = (len(requeued),
                                                       len(failed))
        if requeued or failed:
            log.info("beat: orphan sweep requeued=%s failed=%s",
                     requeued, failed)

        # 2. due sources -> poll_source jobs (dedup via the partial index)
        if settings.poller_enabled:
            sources = await source_dao.list_pollable(
                conn, tuple(services.poller.adapters))
            for source in sources:
                if is_due(source):
                    await services.jobs.enqueue(
                        "poll_source", {"source_id": source.id})
                    counts["polls"] += 1

        # 3. nightly T1 batch
        if await _nightly_due(conn, "enrich_t1_batch",
                              settings.nightly_sweep_utc_hour):
            await services.jobs.enqueue("enrich_t1_batch", {})
            counts["nightly"] = 1

        # 4. brief pre-gen for 7-day-active users (last_login_at is
        # stamped on every login since Phase B), staggered over an hour;
        # inactive users stay on-demand (locked decision).
        if await _nightly_due(conn, "brief_pregen",
                              settings.nightly_sweep_utc_hour):
            cur = await conn.execute(
                "SELECT id FROM app_user WHERE NOT disabled"
                " AND last_login_at >= now() - interval '7 days'"
                " ORDER BY id")
            user_ids = [int(r["id"]) for r in await cur.fetchall()]
            for i, user_id in enumerate(user_ids):
                await services.jobs.enqueue(
                    "brief_generate", {"user_id": user_id},
                    delay_s=i * BRIEF_STAGGER_WINDOW_S / len(user_ids))
                counts["briefs"] += 1

        # 5. due scheduled content -> one 'content_publish' job per item
        # (the partial unique index dedups a re-enqueue while the job runs)
        for item_id in await content_item_dao.list_due(conn):
            await services.jobs.enqueue("content_publish", {"item_id": item_id})
            counts["publishes"] += 1

        # 6. S3 corrections: a single sweep job fans open corrections out to
        # dependent items (the partial unique index keeps it single-flight).
        cur = await conn.execute(
            "SELECT 1 FROM correction WHERE status = 'open' LIMIT 1")
        if await cur.fetchone():
            await services.jobs.enqueue("content_correction", {})
            counts["corrections"] = 1

        # 7. nightly source re-check of documents backing published items
        if await _nightly_due(conn, "source_recheck",
                              settings.nightly_sweep_utc_hour):
            for doc_id in await corrections.due_for_recheck(conn, limit=50):
                await services.jobs.enqueue(
                    "source_recheck", {"document_id": doc_id})
                counts["rechecks"] += 1

        # 8. nightly dynamic source-credibility recompute (S4)
        if await _nightly_due(conn, "credibility_recompute",
                              settings.nightly_sweep_utc_hour):
            await services.jobs.enqueue("credibility_recompute", {})
            counts["credibility"] = 1

        # 9. nightly integrity-eval gate (S5): snapshot live integrity metrics
        # and alert on regression
        if await _nightly_due(conn, "integrity_eval",
                              settings.nightly_sweep_utc_hour):
            await services.jobs.enqueue("integrity_eval", {})
            counts["integrity_eval"] = 1
    return counts


async def _nightly_due(conn: psycopg.AsyncConnection, task: str,
                       hour_utc: int) -> bool:
    """True at most once per UTC day, the first tick at/after HH:00 —
    persisted in beat_run so leader restarts don't double-fire."""
    now = utc_now()
    threshold = f"{now[:10]}T{hour_utc:02d}:00:00.000Z"
    if now < threshold:
        return False
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT last_run_at FROM beat_run WHERE task = %s FOR UPDATE",
            (task,))
        row = await cur.fetchone()
        if row is not None and row["last_run_at"] >= threshold:
            return False
        await conn.execute(
            "INSERT INTO beat_run (task, last_run_at) VALUES (%s, %s)"
            " ON CONFLICT (task) DO UPDATE"
            " SET last_run_at = EXCLUDED.last_run_at",
            (task, now))
    return True
