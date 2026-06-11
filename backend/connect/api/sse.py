"""Shared SSE stream over job_event rows (runtime design §3) — the one
flow both /analyses/{id}/events and /investigations/{id}/events use.

The v0.1 client contract is preserved byte-for-byte: job_event.seq is the
SSE id, replay is ``WHERE job_id = %s AND seq > %s`` (?after= /
Last-Event-ID), a terminal event ends the stream, and a terminal job ROW
without its terminal event (orphan replay) is the safety valve. What
changed is only the wait between queries: subscribe to the EventBus FIRST,
replay rows, then block on bus pings — with a fallback re-query every
``sse_fallback_seconds`` (missed NOTIFY / listener-reconnect backstop;
degrades to v0.1's polling, never breaks). The stream never pins a pooled
connection — one is acquired per query tick.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from fastapi import Request

from connect.orchestration.container import Container
from connect.storage.jobs import TERMINAL_STATUSES

TERMINAL_TYPES = ("done", "error", "cancelled")
# drain cadence while the job row is already terminal (or no bus): v0.1's
# poll interval — keeps terminal-without-event streams closing fast
POLL_SECONDS = 0.2

Translate = Callable[[str, dict], "tuple[str, dict] | None"]


def job_event_stream(request: Request, container: Container, job_id: int,
                     after: int, translate: Translate):
    """An async generator of sse-starlette event dicts for one job."""
    pool = container.pool
    bus = container.bus
    fallback_s = container.settings.sse_fallback_seconds

    async def stream():
        sub = bus.subscribe(job_id) if bus is not None else None
        try:
            last = after
            idle_terminal_polls = 0
            while True:
                async with pool.connection() as conn:
                    cur = await conn.execute(
                        "SELECT seq, type, data FROM job_event"
                        " WHERE job_id = %s AND seq > %s ORDER BY seq",
                        (job_id, last))
                    rows = await cur.fetchall()
                    cur = await conn.execute(
                        "SELECT status FROM job WHERE id = %s", (job_id,))
                    status_row = await cur.fetchone()
                terminal = False
                for row in rows:
                    last = int(row["seq"])
                    data = (row["data"]
                            if isinstance(row["data"], dict) else {})
                    translated = translate(row["type"], data)
                    if row["type"] in TERMINAL_TYPES:
                        terminal = True
                    if translated is None:
                        continue
                    event, payload = translated
                    yield {"id": str(row["seq"]), "event": event,
                           "data": json.dumps(payload)}
                if terminal:
                    break
                # safety valve: job row already terminal (e.g. replay of an
                # orphaned job whose terminal event never landed)
                row_terminal = (status_row is not None
                                and status_row["status"]
                                in TERMINAL_STATUSES)
                if row_terminal:
                    idle_terminal_polls += 1
                    if idle_terminal_polls >= 2 and not rows:
                        break
                if await request.is_disconnected():
                    break
                if sub is None or row_terminal:
                    await asyncio.sleep(POLL_SECONDS)
                else:
                    try:  # live path: wake on the bus ping, drain extras
                        await asyncio.wait_for(sub.get(),
                                               timeout=fallback_s)
                        while not sub.empty():
                            sub.get_nowait()
                    except asyncio.TimeoutError:
                        pass  # fallback re-query bounds staleness
        finally:
            if sub is not None and bus is not None:
                bus.unsubscribe(job_id, sub)

    return stream()
