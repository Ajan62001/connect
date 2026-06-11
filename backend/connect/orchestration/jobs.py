"""Minimal JobRunner: asyncio.create_task + durable job/job_event rows.

No broker — single user, single process. Concurrency is bounded by a
semaphore; startup reconciliation marks orphaned rows failed; shutdown
cancels in-flight tasks (rows flip to 'cancelled')."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Awaitable, Callable

from connect.orchestration import events
from connect.storage import jobs as job_dao

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, conn: sqlite3.Connection, concurrency: int = 2):
        self.conn = conn
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()

    def reconcile_orphans(self) -> int:
        """Startup: jobs left 'running'/'queued' by a dead process fail."""
        return job_dao.reconcile_orphans(self.conn)

    def submit(self, kind: str, payload: dict[str, Any],
               runner: Callable[[], Awaitable[Any]]) -> int:
        """Create the job row and schedule the coroutine; returns job id."""
        job_id = job_dao.create(self.conn, kind, payload)
        task = asyncio.create_task(
            self._run(job_id, runner), name=f"job-{job_id}-{kind}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    def cancel_job(self, job_id: int) -> bool:
        """Cancel one job's in-flight task (Phase 3 analysis cancel).
        Returns False when no live task carries the id (already finished
        or never scheduled in this process)."""
        prefix = f"job-{job_id}-"
        found = False
        for task in list(self._tasks):
            if task.get_name().startswith(prefix) and not task.done():
                task.cancel()
                found = True
        return found

    async def _run(self, job_id: int,
                   runner: Callable[[], Awaitable[Any]]) -> None:
        async with self._semaphore:
            job_dao.mark_running(self.conn, job_id)
            events.emit(self.conn, job_id, "started")
            try:
                result = await runner()
            except asyncio.CancelledError:
                job_dao.mark_finished(self.conn, job_id, "cancelled")
                events.emit(self.conn, job_id, "cancelled")
                raise
            except Exception as e:  # noqa: BLE001 — job failure is data
                log.exception("job %s failed", job_id)
                job_dao.mark_finished(self.conn, job_id, "failed", str(e))
                events.emit(self.conn, job_id, "error", {"error": str(e)})
            else:
                job_dao.mark_finished(self.conn, job_id, "done")
                data = {"result": result} if isinstance(result, (str, int)) else {}
                events.emit(self.conn, job_id, "done", data)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
