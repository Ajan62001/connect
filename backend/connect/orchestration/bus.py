"""EventBus — per-process LISTEN fan-out for job events (runtime design §3).

One dedicated LISTEN connection per api process; SSE endpoints subscribe by
job_id and receive (seq) pings. The payload is a pointer, never the data —
job_event ROWS remain the durable truth, so a dead listener degrades
streams to their fallback re-query (v0.1's polling), never loses events.
The bus reconnects with backoff for as long as it is started.
"""

from __future__ import annotations

import asyncio
import json
import logging

import psycopg

from connect.storage.jobs import CHANNEL_JOB_EVENTS

log = logging.getLogger(__name__)


async def close_quietly(conn: "psycopg.AsyncConnection | None") -> None:
    """Close a connection, swallowing errors (teardown helper)."""
    if conn is None or conn.closed:
        return
    try:
        await conn.close()
    except Exception:  # noqa: BLE001
        pass


class EventBus:
    def __init__(self, dsn: str, *, reconnect_min_s: float = 0.5,
                 reconnect_max_s: float = 10.0):
        self.dsn = dsn
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.connected = asyncio.Event()  # set while the LISTEN conn lives
        self._subs: dict[int, set[asyncio.Queue]] = {}
        self._task: asyncio.Task | None = None
        self._conn: psycopg.AsyncConnection | None = None
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._listen(),
                                             name="job-event-bus")

    async def stop(self) -> None:
        """Deterministic teardown: close the LISTEN connection FIRST (a
        plain task.cancel can be swallowed once inside psycopg's notifies
        wait — observed: the wait resumes and the task hangs), which
        breaks the wait with an OperationalError; the stopping flag stops
        the reconnect loop; cancel-with-retry covers the sleep/connect
        phases."""
        self._stopping = True
        await close_quietly(self._conn)
        task = self._task
        if task is not None:
            for _ in range(5):
                if task.done():
                    break
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task),
                                           timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    break
            else:
                log.error("event bus task did not stop; abandoning it")
            self._task = None
        self.connected.clear()

    # -- subscriptions -----------------------------------------------------------

    def subscribe(self, job_id: int) -> asyncio.Queue:
        """A queue receiving the seq of every new event for job_id."""
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id: int, q: asyncio.Queue) -> None:
        subs = self._subs.get(job_id)
        if subs is not None:
            subs.discard(q)
            if not subs:
                del self._subs[job_id]

    # -- the listener ------------------------------------------------------------

    async def _listen(self) -> None:
        backoff = self.reconnect_min_s
        while not self._stopping:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    self.dsn, autocommit=True)
            except psycopg.Error as e:
                log.warning("event bus connect failed (%s); retry in %.1fs",
                            e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_s)
                continue
            self._conn = conn
            try:
                await conn.execute(f"LISTEN {CHANNEL_JOB_EVENTS}")
                self.connected.set()
                backoff = self.reconnect_min_s
                async for notify in conn.notifies():
                    self._dispatch(notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect forever
                if not self._stopping:
                    log.warning("event bus listener died (%s); reconnecting",
                                e)
            finally:
                self.connected.clear()
                self._conn = None
                await close_quietly(conn)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max_s)

    def _dispatch(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            job_id = int(data["job_id"])
            seq = int(data.get("seq", 0))
        except (ValueError, KeyError, TypeError):
            log.warning("event bus: malformed NOTIFY payload %r", payload)
            return
        for q in self._subs.get(job_id, ()):
            q.put_nowait(seq)
