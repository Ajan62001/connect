"""Job + job_event DAO — the Postgres SKIP LOCKED queue (runtime design §2).

job_event.seq doubles as the SSE event id. Every state change another
process must see promptly carries a ``pg_notify`` in the same transaction
(NOTIFY delivers on commit — exactly the wanted semantics):

- ``job_new``     on enqueue/requeue (payload: kind) — wakes idle workers
- ``job_control`` on cancel (payload: ``{"job_id": N}``) — the owning
  worker cancels its local task
- ``job_events``  on event insert (payload: ``{"job_id": N, "seq": S}``)
  — the api EventBus pings SSE streams

Payloads are pointers, never data (8000-byte NOTIFY cap; rows are the
truth and replayable). NOTIFY is fire-and-forget by design — every
listener has a polling backstop.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import psycopg

from connect.domain.models import Job
from connect.storage.pg import Jsonb, utc_now

CHANNEL_JOB_NEW = "job_new"
CHANNEL_JOB_CONTROL = "job_control"
CHANNEL_JOB_EVENTS = "job_events"

TERMINAL_STATUSES = ("done", "failed", "cancelled")

# Claim returns the full row in this order (the worker needs kind+payload;
# owner_id rides along so execute_job can set the ambient spend user).
_CLAIM_COLUMN_NAMES = ("id", "kind", "priority", "payload", "dossier_id",
                       "status", "attempts", "max_attempts",
                       "cancel_requested", "claimed_by", "owner_id")
_CLAIM_COLUMNS = ", ".join(_CLAIM_COLUMN_NAMES)
# the UPDATE..FROM form must qualify (both job and the CTE carry "id")
_CLAIM_COLUMNS_J = ", ".join(f"j.{c}" for c in _CLAIM_COLUMN_NAMES)


def _to_model(row: Mapping[str, Any]) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        payload=row["payload"] or {},
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


# job kinds that dedup against a partial unique index, keyed by a payload
# field (beat enqueues them blindly on every tick; the index DO-NOTHINGs the
# duplicate). Each MUST have a matching uq_job_* index in storage/schema.py.
_DEDUP_KEYS = {"poll_source": "source_id", "content_publish": "item_id",
               "content_verify": "item_id"}

# the statuses a kind's dedup window covers (must mirror its index predicate).
# content_verify collapses onto QUEUED jobs only: a RUNNING verify snapshotted
# its item row at claim time, so an enqueue for an edit/approve made since
# must survive it — a queued job re-reads everything when it starts.
_DEDUP_STATUS_SQL = "('queued','running')"
_DEDUP_STATUS_SQL_BY_KIND = {"content_verify": "('queued')"}

# job kinds where at most ONE live job may exist globally (the job itself fans
# out over its work set). Dedup against a partial unique index on ((kind)) —
# same schema.py contract as _DEDUP_KEYS.
_SINGLETON_KINDS = frozenset({"content_correction", "reel_factory"})


async def create(conn: psycopg.AsyncConnection, kind: str,
                 payload: dict[str, Any] | None = None,
                 dossier_id: int | None = None, *,
                 priority: int = 50, max_attempts: int = 1,
                 delay_s: float = 0.0, owner_id: int | None = None) -> int:
    """Enqueue: job row + ``job_new`` NOTIFY in one transaction.

    ``owner_id`` is the acting user (NULL = system job) — it drives the
    per-user interactive caps and the ambient spend attribution when a
    worker executes the job.

    ``poll_source`` / ``content_publish`` / ``content_verify`` (keyed by a
    payload field) and the singleton ``content_correction`` dedup against
    their partial unique indexes (at most one live job per key); a deduped
    enqueue returns the EXISTING live job's id and notifies nobody (the job
    is already known).
    """
    dedup_key = _DEDUP_KEYS.get(kind)
    statuses = _DEDUP_STATUS_SQL_BY_KIND.get(kind, _DEDUP_STATUS_SQL)
    conflict = ""
    if dedup_key is not None:
        conflict = (f" ON CONFLICT ((payload->>'{dedup_key}')) WHERE kind ="
                    f" '{kind}' AND status IN {statuses} DO NOTHING")
    elif kind in _SINGLETON_KINDS:
        conflict = (f" ON CONFLICT ((kind)) WHERE kind = '{kind}'"
                    f" AND status IN {statuses} DO NOTHING")
    while True:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO job (kind, priority, payload, dossier_id,"
                " status, max_attempts, created_at, run_at, owner_id)"
                " VALUES (%s,%s,%s,%s,'queued',%s,%s,"
                " now() + make_interval(secs => %s), %s)"
                + conflict + " RETURNING id",
                (kind, priority, Jsonb(payload or {}), dossier_id,
                 max_attempts, utc_now(), delay_s, owner_id))
            row = await cur.fetchone()
            if row is not None:
                await conn.execute("SELECT pg_notify(%s, %s)",
                                   (CHANNEL_JOB_NEW, kind))
                return int(row["id"])
        # deduped: hand back the live job for this key; if it left the dedup
        # window since the conflict, loop and insert again
        if dedup_key is not None:
            cur = await conn.execute(
                f"SELECT id FROM job WHERE kind = '{kind}'"
                f" AND status IN {statuses}"
                f" AND payload->>'{dedup_key}' = %s",
                (str((payload or {})[dedup_key]),))
        else:
            cur = await conn.execute(
                f"SELECT id FROM job WHERE kind = '{kind}'"
                f" AND status IN {statuses}")
        existing = await cur.fetchone()
        if existing is not None:
            return int(existing["id"])


async def get(conn: psycopg.AsyncConnection, job_id: int) -> Job | None:
    cur = await conn.execute(
        "SELECT * FROM job WHERE id = %s", (job_id,))
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def claim_next(conn: psycopg.AsyncConnection, kinds: Sequence[str],
                     worker_id: str) -> dict[str, Any] | None:
    """Claim the best due job of the given kinds — one atomic statement,
    FOR UPDATE SKIP LOCKED, so any number of workers race safely. Returns
    the claimed row (dict) or None when nothing is due."""
    if not kinds:
        return None
    cur = await conn.execute(
        f"""WITH next AS (
              SELECT id FROM job
              WHERE status = 'queued' AND run_at <= now()
                AND kind = ANY(%s)
              ORDER BY priority, run_at, id
              LIMIT 1
              FOR UPDATE SKIP LOCKED
            )
            UPDATE job j SET status = 'running', claimed_by = %s,
                   started_at = %s, heartbeat_at = now(),
                   attempts = attempts + 1
            FROM next WHERE j.id = next.id
            RETURNING {_CLAIM_COLUMNS_J}""",
        (list(kinds), worker_id, utc_now()))
    return await cur.fetchone()


async def claim_one(conn: psycopg.AsyncConnection, job_id: int,
                    worker_id: str) -> dict[str, Any] | None:
    """CAS-claim a SPECIFIC queued job (the in-process queue's path: it
    spawned a task for the id it just enqueued). None = somebody else got
    there first (another worker, or a cancel) — the caller must no-op."""
    cur = await conn.execute(
        f"UPDATE job SET status = 'running', claimed_by = %s,"
        f" started_at = %s, heartbeat_at = now(), attempts = attempts + 1"
        f" WHERE id = %s AND status = 'queued'"
        f" RETURNING {_CLAIM_COLUMNS}",
        (worker_id, utc_now(), job_id))
    return await cur.fetchone()


async def heartbeat(conn: psycopg.AsyncConnection,
                    job_ids: Sequence[int]) -> None:
    if not job_ids:
        return
    await conn.execute(
        "UPDATE job SET heartbeat_at = now()"
        " WHERE id = ANY(%s) AND status = 'running'",
        (list(job_ids),))


async def release(conn: psycopg.AsyncConnection, job_id: int, *,
                  delay_s: float = 5.0) -> None:
    """Put a just-claimed job back (cluster-cap slot unavailable): requeue
    with a small backoff and give the attempt back — it never ran."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE job SET status = 'queued', claimed_by = NULL,"
            " started_at = NULL, heartbeat_at = NULL,"
            " attempts = greatest(attempts - 1, 0),"
            " run_at = now() + make_interval(secs => %s)"
            " WHERE id = %s AND status = 'running'",
            (delay_s, job_id))


async def reclaim_stale(conn: psycopg.AsyncConnection, *,
                        stale_after_s: float = 90.0,
                        ) -> tuple[list[int], list[int]]:
    """The beat's orphan sweep (replaces v0.1 startup reconcile_orphans —
    wrong with >1 process): 'running' jobs whose heartbeat went stale are
    requeued while attempts remain, else failed 'orphaned' with a terminal
    job_event. Returns (requeued_ids, failed_ids)."""
    requeued: list[int] = []
    failed: list[int] = []
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE job SET status = 'queued', claimed_by = NULL,"
            " started_at = NULL, heartbeat_at = NULL, run_at = now()"
            " WHERE status = 'running'"
            " AND heartbeat_at < now() - make_interval(secs => %s)"
            " AND attempts < max_attempts RETURNING id, kind",
            (stale_after_s,))
        for row in await cur.fetchall():
            requeued.append(int(row["id"]))
            await conn.execute("SELECT pg_notify(%s, %s)",
                               (CHANNEL_JOB_NEW, row["kind"]))
        cur = await conn.execute(
            "UPDATE job SET status = 'failed', error = 'orphaned',"
            " finished_at = %s"
            " WHERE status = 'running'"
            " AND heartbeat_at < now() - make_interval(secs => %s)"
            " RETURNING id",
            (utc_now(), stale_after_s))
        failed = [int(r["id"]) for r in await cur.fetchall()]
    for job_id in failed:  # terminal events: own commits, after the CAS
        await insert_event(conn, job_id, "error", {"error": "orphaned"})
    return requeued, failed


async def mark_finished(conn: psycopg.AsyncConnection, job_id: int,
                        status: str, error: str | None = None) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE job SET status=%s, error=%s, finished_at=%s"
            " WHERE id = %s",
            (status, error, utc_now(), job_id))


async def request_cancel(conn: psycopg.AsyncConnection,
                         job_id: int) -> bool:
    """Durable cancellation request: always set the flag (the CancelToken's
    truth); CAS a still-queued job straight to 'cancelled' — it never ran.
    A ``job_control`` NOTIFY nudges whichever worker owns a running job to
    cancel its local task (the flag remains the lost-NOTIFY backstop).
    Returns True when the CAS finished the job here."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE job SET cancel_requested = TRUE WHERE id = %s",
            (job_id,))
        cur = await conn.execute(
            "UPDATE job SET status='cancelled', finished_at=%s"
            " WHERE id = %s AND status = 'queued'", (utc_now(), job_id))
        await conn.execute(
            "SELECT pg_notify(%s, %s)",
            (CHANNEL_JOB_CONTROL, json.dumps({"job_id": job_id})))
    return cur.rowcount > 0


async def insert_event(conn: psycopg.AsyncConnection, job_id: int,
                       type_: str,
                       data: dict[str, Any] | None = None) -> int:
    """One job_event row + its ``job_events`` NOTIFY, committed together
    and immediately (events must never sit invisible inside a long handler
    transaction — risk #3 of the runtime design)."""
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO job_event (job_id, ts, type, data)"
            " VALUES (%s,%s,%s,%s) RETURNING seq",
            (job_id, utc_now(), type_, Jsonb(data or {})))
        row = await cur.fetchone()
        seq = int(row["seq"])
        await conn.execute(
            "SELECT pg_notify(%s, %s)",
            (CHANNEL_JOB_EVENTS,
             json.dumps({"job_id": job_id, "seq": seq})))
    return seq
