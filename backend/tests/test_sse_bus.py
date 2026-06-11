"""Cross-process SSE plumbing: the EventBus receives job_event NOTIFYs as
(job_id, seq) pings over a real LISTEN connection, and the HTTP SSE
endpoints preserve the v0.1 replay contract exactly (?after= /
Last-Event-ID resume, terminal event ends the stream, terminal-row safety
valve) on the bus+replay implementation."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from dbutil import qv
from fastapi.testclient import TestClient

from connect.api.main import create_app
from connect.orchestration import events
from connect.orchestration.bus import EventBus
from connect.storage import jobs as job_dao
from connect.storage.pg import utc_now

pytestmark = pytest.mark.usefixtures("pg_database")


# --- the bus itself --------------------------------------------------------------------


async def test_bus_delivers_event_pings(settings, db):
    bus = EventBus(settings.database_url)
    bus.start()
    try:
        await asyncio.wait_for(bus.connected.wait(), 5)
        job_id = await job_dao.create(db, "analysis", {})
        other_job = await job_dao.create(db, "analysis", {})
        sub = bus.subscribe(job_id)
        seq1 = await events.emit(db, job_id, "stage_started",
                                 {"stage": "normalize"})
        await events.emit(db, other_job, "stage_started", {})  # not ours
        seq2 = await events.emit(db, job_id, "done")
        assert await asyncio.wait_for(sub.get(), 5) == seq1
        assert await asyncio.wait_for(sub.get(), 5) == seq2
        assert sub.empty()  # the other job's ping was filtered out
        bus.unsubscribe(job_id, sub)
    finally:
        await bus.stop()


async def test_bus_stop_is_idempotent_and_clean(settings):
    bus = EventBus(settings.database_url)
    bus.start()
    await asyncio.wait_for(bus.connected.wait(), 5)
    await asyncio.wait_for(bus.stop(), 10)
    await asyncio.wait_for(bus.stop(), 1)  # second stop: no-op
    assert not bus.connected.is_set()


# --- SSE endpoint replay parity over HTTP -------------------------------------------


def parse_sse(text: str) -> list[tuple[int, str, dict]]:
    out = []
    for block in re.split(r"\r?\n\r?\n", text):
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields.setdefault(key.strip(), value.strip())
        if "event" in fields and fields["event"] != "ping":
            out.append((int(fields["id"]), fields["event"],
                        json.loads(fields.get("data") or "{}")))
    return out


@pytest.fixture()
def client_env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


async def seed_finished_analysis(db) -> tuple[int, list[int]]:
    """A completed analysis dossier + job + its event rows, written
    directly (replay must work for jobs no live process remembers)."""
    cur = await db.execute(
        "INSERT INTO dossier (input_text, input_type, status, created_at)"
        " VALUES ('x', 'claim', 'completed', %s) RETURNING id", (utc_now(),))
    dossier_id = int((await cur.fetchone())["id"])
    job_id = await job_dao.create(db, "analysis", {}, dossier_id=dossier_id)
    await job_dao.claim_one(db, job_id, "w1")
    seqs = [
        await events.emit(db, job_id, "started"),
        await events.emit(db, job_id, "stage_started",
                          {"stage": "normalize"}),
        await events.emit(db, job_id, "stage_completed",
                          {"stage": "normalize", "summary": "ok"}),
    ]
    await job_dao.mark_finished(db, job_id, "done")
    seqs.append(await events.emit(db, job_id, "done"))
    return dossier_id, seqs


async def test_sse_replays_rows_and_honors_after(client_env, db):
    client, _container = client_env
    analysis_id, seqs = await seed_finished_analysis(db)

    with client.stream("GET",
                       f"/api/analyses/{analysis_id}/events") as res:
        assert res.status_code == 200
        got = parse_sse("".join(res.iter_text()))
    # 'started' is dropped by translation; ids are the raw seqs
    assert [g[0] for g in got] == seqs[1:]
    assert [g[1] for g in got] == ["stage_started", "stage_completed",
                                   "done"]
    assert got[0][2] == {"stage": "normalize"}

    # ?after= replays only later rows — v0.1 semantics exactly
    with client.stream(
            "GET",
            f"/api/analyses/{analysis_id}/events?after={seqs[2]}") as res:
        resumed = parse_sse("".join(res.iter_text()))
    assert [g[0] for g in resumed] == [seqs[3]]
    assert resumed[0][1] == "done"

    # Last-Event-ID header resumes identically
    with client.stream(
            "GET", f"/api/analyses/{analysis_id}/events",
            headers={"Last-Event-ID": str(seqs[2])}) as res:
        resumed_h = parse_sse("".join(res.iter_text()))
    assert resumed_h == resumed

    # after everything -> empty stream that still terminates
    with client.stream(
            "GET",
            f"/api/analyses/{analysis_id}/events?after={seqs[3]}") as res:
        assert parse_sse("".join(res.iter_text())) == []


async def test_sse_terminal_row_safety_valve(client_env, db):
    """An orphaned job (terminal row, no terminal event) must close the
    stream rather than hang — the v0.1 safety valve on the bus path."""
    client, _container = client_env
    cur = await db.execute(
        "INSERT INTO dossier (input_text, input_type, status, created_at)"
        " VALUES ('x', 'claim', 'failed', %s) RETURNING id", (utc_now(),))
    dossier_id = int((await cur.fetchone())["id"])
    job_id = await job_dao.create(db, "analysis", {},
                                  dossier_id=dossier_id)
    await job_dao.claim_one(db, job_id, "w-dead")
    await events.emit(db, job_id, "stage_started", {"stage": "normalize"})
    await db.execute("UPDATE job SET heartbeat_at = now() - interval"
                     " '10 minutes' WHERE id = %s", (job_id,))
    await job_dao.reclaim_stale(db, stale_after_s=90)
    assert await qv(db, "SELECT status FROM job WHERE id = %s",
                    job_id) == "failed"

    with client.stream("GET",
                       f"/api/analyses/{dossier_id}/events") as res:
        got = parse_sse("".join(res.iter_text()))
    # the sweep wrote the terminal error event; the stream replays + ends
    assert [g[1] for g in got] == ["stage_started", "error"]
    assert got[-1][2] == {"message": "orphaned"}
