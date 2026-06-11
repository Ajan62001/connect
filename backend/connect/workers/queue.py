"""Job queue seam + shared execution discipline (runtime design §2).

Enqueue sites depend on ``JobQueue.enqueue`` / ``request_cancel`` and
nothing more. Two implementations share the DAO:

- ``PgJobQueue`` — enqueue-only (job row + ``job_new`` NOTIFY); execution
  belongs to worker processes (workers/main.py) claiming with FOR UPDATE
  SKIP LOCKED. This is the worker flavor's queue (handlers enqueue
  follow-up jobs) and the api's eventual Phase-C queue.
- ``AsyncioJobQueue`` — the embedded-execution mode (api flavor in dev and
  the no-separate-worker test environment): enqueue ALSO spawns a local
  task which CAS-claims its own job (claim-or-skip, so racing external
  workers can never double-execute) and runs the registered handler.

Priorities (job.priority, claim order ASC): 10 interactive (user clicked),
50 enrichment/background, 90 polls; brief pre-gen sits at 60 — behind
interactive work and enrichment, ahead of polls.

``execute_job`` is the one execution discipline both modes (and the
ImmediateQueue test double) share: the job is ALREADY claimed ('running');
emit 'started', dispatch through the registry, finish with the terminal
state + event exactly as v0.1 did.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any, Protocol

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.orchestration import events
from connect.storage import jobs as job_dao
from connect.workers.registry import CancelToken, WorkerContext, handler_for

log = logging.getLogger(__name__)

PRIORITY_INTERACTIVE = 10
PRIORITY_BACKGROUND = 50
PRIORITY_POLL = 90

KIND_PRIORITIES: dict[str, int] = {
    "analysis": PRIORITY_INTERACTIVE,
    "investigation": PRIORITY_INTERACTIVE,
    "enrich_t2": PRIORITY_INTERACTIVE,    # user clicked "promote"
    "ingest_url": PRIORITY_INTERACTIVE,   # user submitted a URL
    "reverify_claim": PRIORITY_BACKGROUND,
    "enrich_t1_sync": PRIORITY_BACKGROUND,
    "enrich_t1_batch": PRIORITY_BACKGROUND,
    "brief_generate": 60,
    "poll_source": PRIORITY_POLL,
}

# the idempotent batch poll survives worker deaths (design §5). Interactive
# analysis/investigation get ONE retry so a worker crash (kill -9, OOM)
# requeues rather than orphan-fails the user's job — the design's Phase-B
# acceptance ("kill -9 a mid-investigation worker -> job reclaimed").
# Re-run is safe: both handlers reconstruct from the dossier row, section
# writes are upserts (ON CONFLICT (dossier_id, stage)), and the governors
# cap the extra spend. Everything else runs at most once per enqueue.
KIND_MAX_ATTEMPTS: dict[str, int] = {
    "enrich_t1_batch": 3,
    "analysis": 2,
    "investigation": 2,
}


def priority_for(kind: str) -> int:
    return KIND_PRIORITIES.get(kind, PRIORITY_BACKGROUND)


def default_worker_id(suffix: str = "") -> str:
    base = f"{socket.gethostname()}:{os.getpid()}"
    return f"{base}:{suffix}" if suffix else base


class JobQueue(Protocol):
    """What enqueue sites depend on — nothing more."""

    async def enqueue(self, kind: str, payload: dict[str, Any], *,
                      dossier_id: int | None = None,
                      priority: int | None = None,
                      delay_s: float = 0.0) -> int: ...

    async def request_cancel(self, job_id: int) -> bool: ...


async def execute_job(conn: psycopg.AsyncConnection, services: Any,
                      job_id: int, kind: str,
                      payload: dict[str, Any]) -> None:
    """Run one CLAIMED job: registry dispatch + the v0.1 terminal-state /
    event discipline (started -> done|error|cancelled). Shared by both
    queue modes and the ImmediateQueue test double so every path exercises
    the REAL handlers."""
    await events.emit(conn, job_id, "started")
    ctx = WorkerContext(services=services, job_id=job_id, conn=conn,
                        cancel=CancelToken(conn, job_id))
    handler = handler_for(kind)
    try:
        result = await handler(ctx, payload)
    except asyncio.CancelledError:
        await job_dao.mark_finished(conn, job_id, "cancelled")
        await events.emit(conn, job_id, "cancelled")
        raise
    except Exception as e:  # noqa: BLE001 — job failure is data
        log.exception("job %s failed", job_id)
        await job_dao.mark_finished(conn, job_id, "failed", str(e))
        await events.emit(conn, job_id, "error", {"error": str(e)})
    else:
        await job_dao.mark_finished(conn, job_id, "done")
        data = {"result": result} if isinstance(result, (str, int)) else {}
        await events.emit(conn, job_id, "done", data)


class Heartbeater:
    """One task per process: every ``interval_s`` it stamps heartbeat_at on
    this process's running jobs — what keeps the beat's orphan sweep from
    reclaiming live work. Started lazily on the first add (needs a running
    loop)."""

    def __init__(self, pool: AsyncConnectionPool,
                 interval_s: float = 15.0):
        self.pool = pool
        self.interval_s = interval_s
        self._ids: set[int] = set()
        self._task: asyncio.Task | None = None

    def add(self, job_id: int) -> None:
        self._ids.add(job_id)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(),
                                             name="job-heartbeat")

    def discard(self, job_id: int) -> None:
        self._ids.discard(job_id)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            if not self._ids:
                continue
            try:
                async with self.pool.connection() as conn:
                    await job_dao.heartbeat(conn, list(self._ids))
            except Exception:  # noqa: BLE001 — never die; reclaim covers us
                log.exception("heartbeat update failed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


class PgJobQueue:
    """Enqueue-only queue: persist + NOTIFY; worker processes execute."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def enqueue(self, kind: str, payload: dict[str, Any], *,
                      dossier_id: int | None = None,
                      priority: int | None = None,
                      delay_s: float = 0.0) -> int:
        """Create the job row (+ job_new NOTIFY); returns job id."""
        handler_for(kind)  # unknown kind fails HERE, not in a worker
        async with self.pool.connection() as conn:
            return await job_dao.create(
                conn, kind, payload, dossier_id=dossier_id,
                priority=priority_for(kind) if priority is None else priority,
                max_attempts=KIND_MAX_ATTEMPTS.get(kind, 1),
                delay_s=delay_s)

    async def request_cancel(self, job_id: int) -> bool:
        """Set the durable flag + job_control NOTIFY; a still-queued job is
        finished outright (CAS + terminal event — it never ran). Running
        jobs are cancelled by whichever process owns them (NOTIFY fast
        path, CancelToken backstop). Returns True when the CAS finished the
        job here."""
        async with self.pool.connection() as conn:
            finished = await job_dao.request_cancel(conn, job_id)
            if finished:
                await events.emit(conn, job_id, "cancelled")
        return finished


class AsyncioJobQueue(PgJobQueue):
    """Embedded execution: enqueue = job row + local asyncio task (bounded
    by a semaphore). The task CAS-claims its own row first — if an external
    worker (or a cancel) got there first it no-ops, so mixed deployments
    never double-execute. Cancellation keeps the v0.1 fast path
    (task.cancel on locally-owned jobs) on top of the durable flag."""

    def __init__(self, pool: AsyncConnectionPool, services: Any,
                 concurrency: int = 2,
                 heartbeat_interval_s: float = 15.0):
        super().__init__(pool)
        self.services = services
        self.worker_id = default_worker_id("embedded")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()
        self.heartbeater = Heartbeater(pool, heartbeat_interval_s)

    async def enqueue(self, kind: str, payload: dict[str, Any], *,
                      dossier_id: int | None = None,
                      priority: int | None = None,
                      delay_s: float = 0.0) -> int:
        job_id = await super().enqueue(kind, payload, dossier_id=dossier_id,
                                       priority=priority, delay_s=delay_s)
        task = asyncio.create_task(
            self._run(job_id, kind, payload, delay_s=delay_s),
            name=f"job-{job_id}-{kind}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def _run(self, job_id: int, kind: str,
                   payload: dict[str, Any], *, delay_s: float = 0.0) -> None:
        if delay_s > 0:  # honor run_at locally (no claim loop here)
            await asyncio.sleep(delay_s)
        async with self._semaphore:
            async with self.pool.connection() as conn:
                claimed = await job_dao.claim_one(conn, job_id,
                                                  self.worker_id)
                if claimed is None:
                    return  # raced: external worker claimed it / cancelled
                self.heartbeater.add(job_id)
                try:
                    await execute_job(conn, self.services, job_id, kind,
                                      payload)
                finally:
                    self.heartbeater.discard(job_id)

    async def request_cancel(self, job_id: int) -> bool:
        """Durable flag + CAS (super), plus the local task.cancel fast
        path. Returns False when no live (queued or locally-running) job
        carried the id."""
        hit = await super().request_cancel(job_id)
        prefix = f"job-{job_id}-"
        for task in list(self._tasks):
            if task.get_name().startswith(prefix) and not task.done():
                task.cancel()
                hit = True
        return hit

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.heartbeater.stop()
