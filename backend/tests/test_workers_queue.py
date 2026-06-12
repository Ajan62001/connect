"""Phase A queue/registry seam: enqueue(kind, payload) dispatches through
the REAL handler registry (asyncio queue + the ImmediateQueue test double),
unknown kinds fail at the call site, and cancellation is the durable
job.cancel_requested flag (CAS for queued jobs, CancelToken for running
ones)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from dbutil import q1, qall, qv

from kb_factories import ensure_user

from connect.storage import jobs as job_dao
from connect.workers.registry import CancelToken
from connect.workers.testing import ImmediateQueue


class StubEnrichment:
    def __init__(self):
        self.calls: list[int] = []

    async def promote_document(self, conn, document_id: int) -> str:
        self.calls.append(document_id)
        return f"promoted: {document_id}"


async def events_of(conn, job_id):
    return [(r["type"], r["data"] or {}) for r in await qall(
        conn,
        "SELECT type, data FROM job_event WHERE job_id = %s ORDER BY seq",
        job_id)]


# --- fail-fast on unknown kinds ----------------------------------------------------


async def test_enqueue_unknown_kind_fails_fast(container, db):
    with pytest.raises(LookupError):
        await container.jobs.enqueue("bogus_kind", {})
    # nothing leaked into the job table
    assert await qv(db, "SELECT COUNT(*) FROM job") == 0


# --- ImmediateQueue: synchronous dispatch through the real registry -----------------


async def test_immediate_queue_dispatches_real_handler(container, db):
    conn = db
    stub = StubEnrichment()
    queue = ImmediateQueue(container.pool, SimpleNamespace(enrichment=stub))

    job_id = await queue.enqueue("enrich_t2", {"document_id": 42})
    row = await q1(conn, "SELECT * FROM job WHERE id = %s", job_id)
    assert (row["kind"], row["status"]) == ("enrich_t2", "queued")

    assert await queue.run_pending() == [job_id]
    assert stub.calls == [42]
    row = await q1(conn, "SELECT * FROM job WHERE id = %s", job_id)
    assert row["status"] == "done"
    assert await events_of(conn, job_id) == [
        ("started", {}), ("done", {"result": "promoted: 42"})]


async def test_immediate_queue_failure_is_data(container, db):
    conn = db

    class Exploding:
        async def promote_document(self, _conn, document_id):
            raise RuntimeError("boom")

    queue = ImmediateQueue(container.pool,
                           SimpleNamespace(enrichment=Exploding()))
    job_id = await queue.enqueue("enrich_t2", {"document_id": 1})
    await queue.run_pending()
    row = await q1(conn, "SELECT status, error FROM job WHERE id = %s",
                   job_id)
    assert (row["status"], row["error"]) == ("failed", "boom")
    assert (await events_of(conn, job_id))[-1] == \
        ("error", {"error": "boom"})


async def test_immediate_queue_cancel_before_run(container, db):
    conn = db
    stub = StubEnrichment()
    queue = ImmediateQueue(container.pool, SimpleNamespace(enrichment=stub))
    job_id = await queue.enqueue("enrich_t2", {"document_id": 5})

    assert await queue.request_cancel(job_id) is True  # CAS finished it
    row = await q1(conn, "SELECT status, cancel_requested FROM job"
                         " WHERE id = %s", job_id)
    assert (row["status"], row["cancel_requested"]) == ("cancelled", True)
    assert await events_of(conn, job_id) == [("cancelled", {})]

    assert await queue.run_pending() == []  # nothing left to run
    assert stub.calls == []


# --- CancelToken: the durable flag -------------------------------------------------


async def test_cancel_token_reads_flag(db):
    conn = db
    job_id = await job_dao.create(conn, "analysis", {})
    assert await job_dao.claim_one(conn, job_id, "test-worker") is not None

    token = CancelToken(conn, job_id)
    assert await token.cancelled() is False
    await token.raise_if_cancelled()  # no-op while the flag is unset

    # a RUNNING job is not CAS-finished — only flagged
    assert await job_dao.request_cancel(conn, job_id) is False
    assert await qv(conn, "SELECT status FROM job WHERE id = %s",
                    job_id) == "running"
    assert await token.cancelled() is True
    with pytest.raises(asyncio.CancelledError):
        await token.raise_if_cancelled()


# --- AsyncioJobQueue: the production Phase A path -----------------------------------


async def test_asyncio_queue_round_trip(container, db):
    conn = db
    stub = StubEnrichment()
    container.enrichment = stub  # handlers resolve services per dispatch

    job_id = await container.jobs.enqueue("enrich_t2", {"document_id": 7})
    await asyncio.gather(*list(container.jobs._tasks),
                         return_exceptions=True)

    assert stub.calls == [7]
    assert await qv(conn, "SELECT status FROM job WHERE id = %s",
                    job_id) == "done"
    assert await events_of(conn, job_id) == [
        ("started", {}), ("done", {"result": "promoted: 7"})]


async def test_asyncio_queue_enqueue_carries_dossier_id(container, db):
    conn = db
    container.enrichment = StubEnrichment()
    cur = await conn.execute(
        "INSERT INTO dossier (input_text, input_type, status,"
        " created_at, owner_id) VALUES ('x', 'claim', 'pending',"
        " '2026-06-11T00:00:00Z', %s) RETURNING id",
        (await ensure_user(db),))
    dossier_id = int((await cur.fetchone())["id"])
    job_id = await container.jobs.enqueue(
        "enrich_t2", {"document_id": 9}, dossier_id=dossier_id)
    assert await qv(conn, "SELECT dossier_id FROM job WHERE id = %s",
                    job_id) == dossier_id
    await asyncio.gather(*list(container.jobs._tasks),
                         return_exceptions=True)
