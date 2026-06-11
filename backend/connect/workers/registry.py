"""Handler registry — job.kind -> async handler (v0.2 runtime design §2).

A job is (kind, payload); services persist state, enqueue a self-describing
payload, and the registered handler reconstructs the run from rows +
payload. This replaces the v0.1 closure submission (which could never cross
processes). Handlers receive a WorkerContext: the composition root's
services, their own job id (job_event correlation), THE JOB'S CONNECTION
(one per job, acquired by the queue), and a CancelToken.

Cancellation is cooperative and durable: the API sets
``job.cancel_requested``; long handlers check the token at their natural
checkpoints (analysis stage boundaries, investigation loop iterations).
In-process task cancellation remains the low-latency path (queue.py), the
flag is the cross-process truth Phase B relies on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import psycopg

Handler = Callable[["WorkerContext", dict[str, Any]], Awaitable[Any]]

HANDLERS: dict[str, Handler] = {}


def register(kind: str) -> Callable[[Handler], Handler]:
    """Decorator: HANDLERS[kind] = handler. Double registration is a bug."""
    def _decorator(fn: Handler) -> Handler:
        if kind in HANDLERS:
            raise ValueError(
                f"handler for job kind {kind!r} already registered")
        HANDLERS[kind] = fn
        return fn
    return _decorator


def handler_for(kind: str) -> Handler:
    """The handler for a kind; LookupError on an unregistered kind (raised
    at enqueue time — a misspelled kind must fail at the call site, not
    inside a background task)."""
    try:
        return HANDLERS[kind]
    except KeyError:
        raise LookupError(
            f"no handler registered for job kind {kind!r}") from None


class CancelToken:
    """Reads ``job.cancel_requested`` at handler checkpoints. Raising
    asyncio.CancelledError keeps the services' existing cancellation paths
    (except CancelledError -> dossier 'cancelled') identical whether the
    cancel arrived via task.cancel or via the durable flag."""

    def __init__(self, conn: psycopg.AsyncConnection, job_id: int):
        self._conn = conn
        self.job_id = job_id

    async def cancelled(self) -> bool:
        cur = await self._conn.execute(
            "SELECT cancel_requested FROM job WHERE id = %s",
            (self.job_id,))
        row = await cur.fetchone()
        return bool(row["cancel_requested"]) if row is not None else False

    async def raise_if_cancelled(self) -> None:
        if await self.cancelled():
            raise asyncio.CancelledError(
                f"job {self.job_id} cancel requested")


@dataclass(frozen=True)
class WorkerContext:
    """What a handler gets besides its payload."""
    services: Any  # the composition root (orchestration.container.Container)
    job_id: int
    conn: psycopg.AsyncConnection  # THE job's connection (one per job)
    cancel: CancelToken
