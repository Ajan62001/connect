"""Worker entrypoint — ``python -m connect.workers.main`` (runtime design
§1/§2). Claims jobs from the Postgres SKIP LOCKED queue and runs the
registered handlers; every worker also runs the beat candidacy coroutine
(exactly one leader cluster-wide via advisory lock).

Concurrency is three-layered:
- a local total (``worker_concurrency``) bounds this process;
- a local per-kind map (``worker_kind_caps``) keeps expensive kinds from
  monopolizing the process — kinds at their cap are simply EXCLUDED from
  the next claim's ``kind = ANY(...)``;
- cluster-wide caps (``worker_cluster_caps``) for the truly expensive
  kinds use advisory-lock slots taken on the job's own connection
  (session-scoped: crash-safe); a claim that can't get a slot is released
  back to the queue with a 5s backoff.

Wake-up: LISTEN job_new (enqueues NOTIFY on commit) with a 1s poll as the
missed-NOTIFY backstop. Cancellation: LISTEN job_control cancels the local
task when this process owns the job; the durable cancel_requested flag +
CancelToken remain the backstop. Heartbeats every 15s keep the beat's
orphan sweep off our live jobs; a SIGKILLed worker's jobs are reclaimed by
the sweep (requeue while attempts < max_attempts, else failed 'orphaned').
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import zlib
from collections import Counter
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.storage import jobs as job_dao
from connect.storage.jobs import CHANNEL_JOB_CONTROL, CHANNEL_JOB_NEW
from connect.workers import beat
from connect.workers.queue import Heartbeater, default_worker_id, execute_job
from connect.workers.registry import HANDLERS

log = logging.getLogger(__name__)

# int4 namespace for cluster-cap slot locks: ASCII "cncs".
KIND_SLOT_LOCK_NS = 0x636E6373

RELEASE_BACKOFF_S = 5.0


def _slot_key(kind: str, slot: int) -> int:
    """int4 key2 for (kind, slot): crc32(kind) high bits | slot (< 256)."""
    return (zlib.crc32(kind.encode()) & 0x7FFFFF00) | slot


async def acquire_kind_slot(conn: psycopg.AsyncConnection, kind: str,
                            cap: int) -> int | None:
    """Try the kind's cluster slots on THIS connection (held for the job's
    whole run; released in release_kind_slot or by session death)."""
    for slot in range(cap):
        cur = await conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS got",
            (KIND_SLOT_LOCK_NS, _slot_key(kind, slot)))
        if bool((await cur.fetchone())["got"]):
            return slot
    return None


async def release_kind_slot(conn: psycopg.AsyncConnection, kind: str,
                            slot: int) -> None:
    await conn.execute("SELECT pg_advisory_unlock(%s, %s)",
                       (KIND_SLOT_LOCK_NS, _slot_key(kind, slot)))


class Worker:
    """One claim-and-execute process (N per deployment)."""

    def __init__(self, pool: AsyncConnectionPool, services: Any, *,
                 dsn: str, worker_id: str | None = None,
                 concurrency: int = 6,
                 kind_caps: dict[str, int] | None = None,
                 kind_cap_default: int = 4,
                 cluster_caps: dict[str, int] | None = None,
                 claim_interval_s: float = 1.0,
                 heartbeat_interval_s: float = 15.0,
                 run_beat: bool = True):
        self.pool = pool
        self.services = services
        self.dsn = dsn
        self.worker_id = worker_id or default_worker_id()
        self.concurrency = concurrency
        self.kind_caps = dict(kind_caps or {})
        self.kind_cap_default = kind_cap_default
        self.cluster_caps = dict(cluster_caps or {})
        self.claim_interval_s = claim_interval_s
        self.run_beat = run_beat
        self.heartbeater = Heartbeater(pool, heartbeat_interval_s)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._running: dict[int, asyncio.Task] = {}
        self._running_kinds: Counter[str] = Counter()
        self._listen_conn: psycopg.AsyncConnection | None = None

    @classmethod
    def from_container(cls, container: Any, **overrides: Any) -> "Worker":
        s = container.settings
        kwargs: dict[str, Any] = dict(
            dsn=s.database_url,
            concurrency=s.worker_concurrency,
            kind_caps=s.worker_kind_caps,
            kind_cap_default=s.worker_kind_cap_default,
            cluster_caps=s.worker_cluster_caps,
            heartbeat_interval_s=s.heartbeat_seconds,
        )
        kwargs.update(overrides)
        return cls(container.pool, container, **kwargs)

    # -- capacity ----------------------------------------------------------------

    def eligible_kinds(self) -> list[str]:
        """Registered kinds with a free local slot — what the next claim's
        ``kind = ANY(...)`` carries. Empty when the process is full."""
        if len(self._running) >= self.concurrency:
            return []
        return [k for k in HANDLERS
                if self._running_kinds[k]
                < self.kind_caps.get(k, self.kind_cap_default)]

    # -- lifecycle ---------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        """Claim loop until stopped. Wakes on job_new NOTIFY or every
        ``claim_interval_s`` (missed-NOTIFY backstop)."""
        log.info("worker %s up (concurrency=%s, kinds=%s)",
                 self.worker_id, self.concurrency, sorted(HANDLERS))
        listener = asyncio.create_task(self._listen(),
                                       name="worker-listener")
        beat_task = (asyncio.create_task(
            beat.beat_candidate(self.services, self._stop),
            name="beat-candidate") if self.run_beat else None)
        try:
            while not self._stop.is_set():
                claimed = await self._claim_and_spawn()
                if claimed:
                    continue  # immediately try to fill remaining capacity
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           timeout=self.claim_interval_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._stop.set()  # also reached on claim-loop crash
            # close the LISTEN connection FIRST: a lone task.cancel can be
            # swallowed inside psycopg's notifies wait (see EventBus.stop)
            from connect.orchestration.bus import close_quietly
            await close_quietly(self._listen_conn)
            listener.cancel()
            for task in (listener, beat_task):
                if task is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(task),
                                               timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError,
                            Exception):  # noqa: BLE001
                        pass
            # v0.1 shutdown semantics (JobRunner parity): cancel running
            # jobs — they finish 'cancelled'. A SIGKILL (no shutdown at
            # all) leaves 'running' rows for the orphan sweep instead;
            # the bounded wait keeps a cancel swallowed mid-psycopg-wait
            # from wedging shutdown (the sweep reclaims those too).
            for task in list(self._running.values()):
                task.cancel()
            if self._running:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self._running.values(),
                                       return_exceptions=True),
                        timeout=10.0)
                except asyncio.TimeoutError:
                    log.error("running jobs did not stop in 10s;"
                              " leaving them to the orphan sweep")
            await self.heartbeater.stop()
            log.info("worker %s stopped", self.worker_id)

    # -- claiming ----------------------------------------------------------------

    async def _claim_and_spawn(self) -> bool:
        kinds = self.eligible_kinds()
        if not kinds:
            return False
        try:
            async with self.pool.connection() as conn:
                row = await job_dao.claim_next(conn, kinds, self.worker_id)
        except Exception:  # noqa: BLE001 — DB blip; back off via the wait
            log.exception("claim failed")
            return False
        if row is None:
            return False
        job_id, kind = int(row["id"]), row["kind"]
        task = asyncio.create_task(self._run_job(job_id, kind,
                                                 row["payload"] or {}),
                                   name=f"job-{job_id}-{kind}")
        self._running[job_id] = task
        self._running_kinds[kind] += 1
        task.add_done_callback(
            lambda _t, job_id=job_id, kind=kind: self._job_done(job_id,
                                                                kind))
        return True

    def _job_done(self, job_id: int, kind: str) -> None:
        self._running.pop(job_id, None)
        self._running_kinds[kind] -= 1
        self._wake.set()  # capacity freed — claim again now

    async def _run_job(self, job_id: int, kind: str,
                       payload: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:  # THE job's connection
            slot: int | None = None
            cap = self.cluster_caps.get(kind)
            if cap is not None:
                slot = await acquire_kind_slot(conn, kind, cap)
                if slot is None:  # cluster is full — give the job back
                    await job_dao.release(conn, job_id,
                                          delay_s=RELEASE_BACKOFF_S)
                    return
            self.heartbeater.add(job_id)
            try:
                await execute_job(conn, self.services, job_id, kind,
                                  payload)
            except asyncio.CancelledError:
                pass  # terminal state already written by execute_job
            finally:
                self.heartbeater.discard(job_id)
                if slot is not None:
                    await release_kind_slot(conn, kind, slot)

    # -- LISTEN (wake-ups + cross-process cancellation) ----------------------------

    async def _listen(self) -> None:
        from connect.orchestration.bus import close_quietly

        backoff = 0.5
        while not self._stop.is_set():
            try:
                conn = await psycopg.AsyncConnection.connect(
                    self.dsn, autocommit=True)
            except psycopg.Error as e:
                log.warning("worker listener connect failed (%s)", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            self._listen_conn = conn
            try:
                await conn.execute(f"LISTEN {CHANNEL_JOB_NEW}")
                await conn.execute(f"LISTEN {CHANNEL_JOB_CONTROL}")
                backoff = 0.5
                async for notify in conn.notifies():
                    if notify.channel == CHANNEL_JOB_NEW:
                        self._wake.set()
                    elif notify.channel == CHANNEL_JOB_CONTROL:
                        self._on_control(notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect forever
                if not self._stop.is_set():
                    log.warning("worker listener died (%s); reconnecting", e)
            finally:
                self._listen_conn = None
                await close_quietly(conn)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _on_control(self, payload: str) -> None:
        try:
            job_id = int(json.loads(payload)["job_id"])
        except (ValueError, KeyError, TypeError):
            log.warning("malformed job_control payload %r", payload)
            return
        task = self._running.get(job_id)
        if task is not None and not task.done():
            log.info("cancelling job %s (job_control)", job_id)
            task.cancel()


# -- entrypoint -------------------------------------------------------------------


async def amain() -> None:
    from connect.orchestration.config import Settings
    from connect.orchestration.container import Container

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    container = Container(Settings(), flavor="worker")
    await container.startup()
    worker = Worker.from_container(container)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.request_stop)
    try:
        await worker.run()
    finally:
        await container.shutdown()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
