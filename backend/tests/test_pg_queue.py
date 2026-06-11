"""Phase B queue runtime: SKIP LOCKED claims (priority order, kind
filters, run_at visibility), poll dedup, heartbeat/orphan reclaim, the
cluster-cap advisory slots, and two REAL concurrent Worker processes-in-
miniature (claim loops + LISTEN) executing without double-claims and
cancelling across "processes" via job_control NOTIFY."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from dbutil import q1, qall, qv

from connect.storage import jobs as job_dao
from connect.workers.main import (
    Worker,
    acquire_kind_slot,
    release_kind_slot,
)
from connect.workers.queue import PgJobQueue, priority_for

pytestmark = pytest.mark.usefixtures("pg_database")


# --- handler-side stubs (job kinds carry a CHECK constraint, so tests run
# real kinds against stub services) -------------------------------------------------


class RecordingEnrichment:
    """enrich_t2 stub: records every execution; optionally blocks."""

    def __init__(self, block: bool = False, work_s: float = 0.0):
        self.calls: list[int] = []
        self.block = block
        self.work_s = work_s
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def promote_document(self, _conn, document_id: int) -> str:
        self.calls.append(document_id)
        self.started.set()
        if self.block:
            await self.release.wait()
        elif self.work_s:
            await asyncio.sleep(self.work_s)
        return f"promoted: {document_id}"


def services_with(stub) -> SimpleNamespace:
    return SimpleNamespace(enrichment=stub)


async def wait_for(predicate, timeout=8.0, interval=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


# --- claim order: interactive > background > polls, FIFO within ---------------------


async def test_claim_priority_order(db):
    # enqueue in "wrong" order; priorities come from the kind map
    ids = {}
    for kind in ("poll_source", "enrich_t1_sync", "analysis"):
        payload = {"source_id": 1} if kind == "poll_source" else {}
        ids[kind] = await job_dao.create(db, kind, payload,
                                         priority=priority_for(kind))
    kinds = ["poll_source", "enrich_t1_sync", "analysis"]
    claimed = [await job_dao.claim_next(db, kinds, "w1") for _ in range(3)]
    assert [c["kind"] for c in claimed] == [
        "analysis", "enrich_t1_sync", "poll_source"]  # 10 < 50 < 90
    assert await job_dao.claim_next(db, kinds, "w1") is None
    row = await q1(db, "SELECT * FROM job WHERE id = %s", ids["analysis"])
    assert (row["status"], row["claimed_by"], row["attempts"]) == \
        ("running", "w1", 1)
    assert row["heartbeat_at"] is not None and row["started_at"] is not None


async def test_claim_fifo_within_priority_and_kind_filter(db):
    first = await job_dao.create(db, "enrich_t2", {"document_id": 1},
                                 priority=50)
    second = await job_dao.create(db, "enrich_t2", {"document_id": 2},
                                  priority=50)
    await job_dao.create(db, "analysis", {}, priority=10)
    # a worker with no free 'analysis' slot simply excludes the kind
    claimed = await job_dao.claim_next(db, ["enrich_t2"], "w1")
    assert int(claimed["id"]) == first
    claimed = await job_dao.claim_next(db, ["enrich_t2"], "w1")
    assert int(claimed["id"]) == second


async def test_run_at_future_is_invisible(db):
    await job_dao.create(db, "brief_generate", {}, priority=60,
                         delay_s=3600.0)
    assert await job_dao.claim_next(db, ["brief_generate"], "w1") is None


async def test_poll_source_dedup_returns_live_job(db):
    queue_id = await job_dao.create(db, "poll_source", {"source_id": 7},
                                    priority=90)
    again = await job_dao.create(db, "poll_source", {"source_id": 7},
                                 priority=90)
    assert again == queue_id
    other = await job_dao.create(db, "poll_source", {"source_id": 8},
                                 priority=90)
    assert other != queue_id
    assert await qv(db, "SELECT COUNT(*) FROM job") == 2
    # a finished poll no longer blocks a new one
    await job_dao.claim_one(db, queue_id, "w1")
    await job_dao.mark_finished(db, queue_id, "done")
    fresh = await job_dao.create(db, "poll_source", {"source_id": 7},
                                 priority=90)
    assert fresh not in (queue_id, other)


# --- heartbeats + orphan reclaim ----------------------------------------------------


async def test_reclaim_stale_requeues_then_fails(db):
    job_id = await job_dao.create(db, "enrich_t1_batch", {},
                                  max_attempts=2)
    assert await job_dao.claim_one(db, job_id, "w-dead") is not None
    # a live heartbeat is never reclaimed
    assert await job_dao.reclaim_stale(db, stale_after_s=90) == ([], [])
    # kill -9 simulation: heartbeat goes stale
    await db.execute("UPDATE job SET heartbeat_at = now() - interval"
                     " '10 minutes' WHERE id = %s", (job_id,))
    requeued, failed = await job_dao.reclaim_stale(db, stale_after_s=90)
    assert (requeued, failed) == ([job_id], [])
    row = await q1(db, "SELECT * FROM job WHERE id = %s", job_id)
    assert (row["status"], row["claimed_by"], row["attempts"]) == \
        ("queued", None, 1)
    # second death exhausts max_attempts -> failed 'orphaned' + event
    assert await job_dao.claim_next(db, ["enrich_t1_batch"], "w-dead2")
    await db.execute("UPDATE job SET heartbeat_at = now() - interval"
                     " '10 minutes' WHERE id = %s", (job_id,))
    requeued, failed = await job_dao.reclaim_stale(db, stale_after_s=90)
    assert (requeued, failed) == ([], [job_id])
    row = await q1(db, "SELECT status, error FROM job WHERE id = %s",
                   job_id)
    assert (row["status"], row["error"]) == ("failed", "orphaned")
    events = await qall(db, "SELECT type, data FROM job_event"
                            " WHERE job_id = %s ORDER BY seq", job_id)
    assert events[-1]["type"] == "error"
    assert events[-1]["data"] == {"error": "orphaned"}


async def test_release_gives_the_attempt_back(db):
    job_id = await job_dao.create(db, "analysis", {}, priority=10)
    assert await job_dao.claim_one(db, job_id, "w1") is not None
    await job_dao.release(db, job_id, delay_s=60.0)
    row = await q1(db, "SELECT * FROM job WHERE id = %s", job_id)
    assert (row["status"], row["attempts"], row["claimed_by"]) == \
        ("queued", 0, None)
    # backoff: invisible to claims until run_at passes
    assert await job_dao.claim_next(db, ["analysis"], "w2") is None


# --- cluster-wide caps: advisory-lock slots ------------------------------------------


async def test_cluster_slots_cap_across_sessions(db, pool):
    async with pool.connection() as other:
        assert await acquire_kind_slot(db, "investigation", 2) == 0
        assert await acquire_kind_slot(other, "investigation", 2) == 1
        # cluster full: a third claimer gets no slot
        async with pool.connection() as third:
            assert await acquire_kind_slot(third, "investigation", 2) is None
        await release_kind_slot(db, "investigation", 0)
        async with pool.connection() as third:
            assert await acquire_kind_slot(third, "investigation", 2) == 0
            await release_kind_slot(third, "investigation", 0)
        await release_kind_slot(other, "investigation", 1)


# --- two concurrent workers ----------------------------------------------------------


def make_worker(pool, settings, stub, **overrides) -> Worker:
    kwargs = dict(
        dsn=settings.database_url,
        concurrency=3,
        kind_cap_default=3,
        cluster_caps={},
        claim_interval_s=0.05,
        heartbeat_interval_s=0.5,
        run_beat=False,
    )
    kwargs.update(overrides)
    return Worker(pool, services_with(stub), **kwargs)


async def test_two_workers_each_job_runs_exactly_once(db, pool, settings):
    stub = RecordingEnrichment(work_s=0.02)
    queue = PgJobQueue(pool)
    w1 = make_worker(pool, settings, stub, worker_id="w1")
    w2 = make_worker(pool, settings, stub, worker_id="w2")
    t1 = asyncio.create_task(w1.run())
    t2 = asyncio.create_task(w2.run())
    try:
        job_ids = [await queue.enqueue("enrich_t2", {"document_id": i})
                   for i in range(10)]
        await wait_for(lambda: q1(
            db, "SELECT 1 WHERE %s = (SELECT COUNT(*) FROM job"
                " WHERE status = 'done')", len(job_ids)))
    finally:
        w1.request_stop()
        w2.request_stop()
        await asyncio.gather(t1, t2)
    # every job executed exactly once, despite two racing claim loops
    assert sorted(stub.calls) == list(range(10))
    rows = await qall(db, "SELECT id, status, attempts, claimed_by"
                          " FROM job ORDER BY id")
    assert all(r["status"] == "done" and r["attempts"] == 1 for r in rows)
    assert {r["claimed_by"] for r in rows} <= {"w1", "w2"}
    # terminal events all landed
    assert await qv(db, "SELECT COUNT(*) FROM job_event"
                        " WHERE type = 'done'") == 10


async def test_cancel_running_job_across_processes(db, pool, settings):
    """The api-side queue cancels a job OWNED by a separate worker: the
    durable flag + job_control NOTIFY; the worker's listener cancels the
    local task and the job finishes 'cancelled'."""
    stub = RecordingEnrichment(block=True)
    queue = PgJobQueue(pool)  # "the api process"
    worker = make_worker(pool, settings, stub, worker_id="w1")
    task = asyncio.create_task(worker.run())
    try:
        job_id = await queue.enqueue("enrich_t2", {"document_id": 5})
        await asyncio.wait_for(stub.started.wait(), timeout=8.0)
        assert await qv(db, "SELECT status FROM job WHERE id = %s",
                        job_id) == "running"
        finished_here = await queue.request_cancel(job_id)
        assert finished_here is False  # running elsewhere, not CAS-able
        await wait_for(lambda: q1(
            db, "SELECT 1 FROM job WHERE id = %s AND status = 'cancelled'",
            job_id))
        events = await qall(db, "SELECT type FROM job_event"
                                " WHERE job_id = %s ORDER BY seq", job_id)
        assert [e["type"] for e in events] == ["started", "cancelled"]
    finally:
        stub.release.set()
        worker.request_stop()
        await task


async def test_worker_respects_local_kind_cap(db, pool, settings):
    """A kind at its local cap is excluded from claims; other kinds keep
    flowing."""
    stub = RecordingEnrichment(block=True)
    worker = make_worker(pool, settings, stub, worker_id="w1",
                         concurrency=3, kind_caps={"enrich_t2": 1})
    queue = PgJobQueue(pool)
    task = asyncio.create_task(worker.run())
    try:
        first = await queue.enqueue("enrich_t2", {"document_id": 1})
        await asyncio.wait_for(stub.started.wait(), timeout=8.0)
        second = await queue.enqueue("enrich_t2", {"document_id": 2})
        await asyncio.sleep(0.3)  # several claim intervals
        # cap=1: the second job stays queued while the first blocks
        assert await qv(db, "SELECT status FROM job WHERE id = %s",
                        second) == "queued"
        assert "enrich_t2" not in worker.eligible_kinds()
        stub.release.set()
        await wait_for(lambda: q1(
            db, "SELECT 1 WHERE 2 = (SELECT COUNT(*) FROM job"
                " WHERE status = 'done')"))
        assert sorted(stub.calls) == [1, 2]
        assert first != second
    finally:
        worker.request_stop()
        await task


async def test_worker_releases_job_when_cluster_full(db, pool, settings):
    """Cluster cap 1 held by another session: the worker claims, fails the
    slot, and releases the job back with backoff (attempts unburned)."""
    job_id = await job_dao.create(db, "analysis", {}, priority=10)
    # an "investigation elsewhere" holds the only analysis slot
    assert await acquire_kind_slot(db, "analysis", 1) == 0
    stub = RecordingEnrichment()
    worker = make_worker(pool, settings, stub, worker_id="w1",
                         cluster_caps={"analysis": 1})
    task = asyncio.create_task(worker.run())
    try:
        await wait_for(lambda: q1(
            db, "SELECT 1 FROM job WHERE id = %s AND status = 'queued'"
                " AND run_at > now()", job_id))
        row = await q1(db, "SELECT attempts FROM job WHERE id = %s",
                       job_id)
        assert row["attempts"] == 0
    finally:
        worker.request_stop()
        await task
        await release_kind_slot(db, "analysis", 0)
    # no execution events: the job never ran
    assert await qv(db, "SELECT COUNT(*) FROM job_event"
                        " WHERE job_id = %s", job_id) == 0
