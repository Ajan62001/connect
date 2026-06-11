"""ImmediateQueue — the in-process test double (runtime design §2).

Implements the same ``enqueue()`` / ``request_cancel()`` seam as the real
queue but defers execution to an explicit ``run_pending()``, which
CAS-claims each row and dispatches through the REAL handler registry
(execute_job) — tests keep a deterministic flow with no event-loop timing
while still exercising the production dispatch path.
"""

from __future__ import annotations

import asyncio
from typing import Any

from psycopg_pool import AsyncConnectionPool

from connect.workers.queue import PgJobQueue, execute_job
from connect.storage import jobs as job_dao


class ImmediateQueue(PgJobQueue):
    def __init__(self, pool: AsyncConnectionPool, services: Any):
        super().__init__(pool)
        self.services = services
        self._pending: list[tuple[int, str, dict[str, Any]]] = []

    async def enqueue(self, kind: str, payload: dict[str, Any], *,
                      dossier_id: int | None = None,
                      priority: int | None = None,
                      delay_s: float = 0.0) -> int:
        job_id = await super().enqueue(kind, payload, dossier_id=dossier_id,
                                       priority=priority, delay_s=delay_s)
        self._pending.append((job_id, kind, dict(payload)))
        return job_id

    async def request_cancel(self, job_id: int) -> bool:
        finished = await super().request_cancel(job_id)
        if finished:
            self._pending = [p for p in self._pending if p[0] != job_id]
        return finished

    async def run_pending(self) -> list[int]:
        """Dispatch every pending job through the real registry; returns
        the job ids that ran (cancelled-while-queued jobs never appear —
        the claim CAS finds their rows already terminal)."""
        ran: list[int] = []
        while self._pending:
            job_id, kind, payload = self._pending.pop(0)
            try:
                async with self.pool.connection() as conn:
                    claimed = await job_dao.claim_one(conn, job_id,
                                                      "immediate-queue")
                    if claimed is None:
                        continue
                    await execute_job(conn, self.services, job_id, kind,
                                      payload)
            except asyncio.CancelledError:
                pass  # the job row is already terminal ('cancelled')
            ran.append(job_id)
        return ran
