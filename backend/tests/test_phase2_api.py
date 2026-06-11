"""Phase 2 API surfaces: event/thread detail, cursors + entity delta math,
calendar seed + window query, the manual promote endpoint (job round-trip
through a MockProvider), and the document-detail event field."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from kb_factories import add_mentions, t1_doc
from mock_llm import MockProvider

from connect.api.main import create_app
from connect.knowledge.calendar import CALENDAR_SEED, seed_calendar_events
from connect.knowledge.enrichment.t1 import EnrichmentT1, T1Entity
from connect.knowledge.linking import event_clusterer as ec
from connect.llm.spend import Governor
from connect.storage.pg import utc_now
from dbutil import q1, qv


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


async def wait_for_job(conn, job_id, timeout=5.0):
    import asyncio
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await q1(conn, "SELECT status, error FROM job WHERE id=%s",
                       job_id)
        if row and row["status"] in ("done", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


async def cluster_one(container, conn, **kwargs) -> int:
    doc = await t1_doc(conn, **kwargs)
    result = await ec.assign_document(conn, None,
                                      Governor(container.pool, 2.0), doc)
    return result.event_id, doc


# --- events / threads --------------------------------------------------------------

async def test_event_detail_endpoint(env, db):
    client, container = env
    conn = db
    event_id, doc = await cluster_one(
        container, conn, title="RBI hikes rates",
        summary="RBI raised the repo rate.",
        event_type="rbi_action", entities=("RBI", "Repo Rate"))
    res = client.get(f"/api/events/{event_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["event"]["id"] == event_id
    assert body["event"]["title"] == "RBI hikes rates"
    assert body["event"]["summary"] == "RBI raised the repo rate."
    assert body["event"]["event_type"] == "rbi_action"
    assert body["event"]["doc_count"] == 1
    assert body["event"]["story_id"] is None
    assert [d["id"] for d in body["documents"]] == [doc]
    assert {e["name"] for e in body["entities"]} == {"RBI", "Repo Rate"}
    assert client.get("/api/events/9999").status_code == 404


async def test_thread_detail_endpoint(env, db):
    client, container = env
    conn = db
    a, _ = await cluster_one(container, conn, title="Bill introduced",
                             event_type="bill_stage",
                             entities=("Bill X", "MeitY"),
                             published_at="2026-06-01T00:00:00Z")
    b, _ = await cluster_one(container, conn, title="Different matter",
                             event_type="court_ruling",
                             entities=("Supreme Court",),
                             published_at="2026-06-09T00:00:00Z")
    from connect.knowledge.linking import story_threader as st
    story_id = await st.merge_into_story(conn, b, a)

    res = client.get(f"/api/threads/{story_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["story"]["id"] == story_id
    assert body["story"]["title"] == "Bill introduced"
    assert body["story"]["status"] == "active"
    assert body["story"]["doc_count"] == 2
    # chronological member events
    assert [e["id"] for e in body["events"]] == [a, b]
    assert body["events"][0]["occurred_on"] == "2026-06-01"
    names = {e["name"] for e in body["entities"]}
    assert {"Bill X", "MeitY", "Supreme Court"} <= names
    assert body["summary"] is None
    assert client.get("/api/threads/9999").status_code == 404


# --- cursors + entity delta -----------------------------------------------------------

def test_cursor_validation(env):
    client, _ = env
    assert client.post("/api/cursors",
                       json={"surface": "bogus", "ref_id": 1}
                       ).status_code == 422
    assert client.post("/api/cursors",
                       json={"surface": "entity", "ref_id": 1}
                       ).status_code == 204


async def test_entity_delta_math(env, db):
    client, container = env
    conn = db
    doc1 = await t1_doc(conn, title="Doc 1",
                        entities=("SEBI", "Adani Group"))
    entity_id = await qv(conn, "SELECT id FROM entity WHERE name='SEBI'")

    # no cursor yet -> delta is null
    body = client.get(f"/api/entities/{entity_id}").json()
    assert body["delta"] is None

    assert client.post("/api/cursors", json={
        "surface": "entity", "ref_id": entity_id}).status_code == 204
    body = client.get(f"/api/entities/{entity_id}").json()
    assert body["delta"] == {"events": 0, "documents": 0, "claims": 0}

    # backdate the cursor, then land new knowledge
    await conn.execute("UPDATE view_cursor SET last_seen_at="
                       " '2026-01-01T00:00:00.000Z' WHERE surface='entity'")
    doc2 = await t1_doc(conn, title="Doc 2", entities=("SEBI",))
    await ec.assign_document(conn, None, Governor(container.pool, 2.0),
                             doc2)
    cur = await conn.execute(
        "INSERT INTO claim (text, first_document_id, created_at)"
        " VALUES ('c', %s, %s) RETURNING id", (doc2, utc_now()))
    claim_id = (await cur.fetchone())["id"]
    await conn.execute(
        "INSERT INTO claim_sighting (claim_id, document_id, stance,"
        " created_at) VALUES (%s, %s, 'asserts', %s)",
        (claim_id, doc2, utc_now()))

    body = client.get(f"/api/entities/{entity_id}").json()
    # doc1 + doc2 mentions are both newer than the backdated cursor
    assert body["delta"] == {"events": 1, "documents": 2, "claims": 1}
    # the events panel lists the entity's recent events
    assert len(body["events"]) == 1
    assert body["events"][0]["title"] == "Doc 2"


# --- calendar ---------------------------------------------------------------------------

async def test_calendar_seed_idempotent_and_window(env, db):
    client, container = env
    conn = db
    count = await qv(conn, "SELECT COUNT(*) FROM calendar_event")
    assert count == len(CALENDAR_SEED)
    assert await seed_calendar_events(conn) == 0  # second pass adds nothing
    assert await qv(conn, "SELECT COUNT(*) FROM calendar_event") == count

    # synthetic rows pin the window behavior regardless of today's date
    await conn.execute(
        "INSERT INTO calendar_event (kind, scope, occurs_on, label)"
        " SELECT 'other', 'national', current_date + 10, 'in-window'")
    await conn.execute(
        "INSERT INTO calendar_event (kind, scope, occurs_on, label)"
        " SELECT 'other', 'national', current_date + 200, 'out-of-window'")
    await conn.execute(
        "INSERT INTO calendar_event (kind, scope, occurs_on, ends_on,"
        " label) SELECT 'other', 'national', current_date - 5,"
        " current_date + 2, 'in-progress'")
    await conn.execute(
        "INSERT INTO calendar_event (kind, scope, occurs_on, label)"
        " SELECT 'other', 'national', current_date - 30, 'long-past'")

    labels = [e["label"] for e in client.get("/api/calendar?days=120").json()]
    assert "in-window" in labels
    assert "in-progress" in labels
    assert "out-of-window" not in labels
    assert "long-past" not in labels
    # soonest-first ordering
    assert labels.index("in-progress") < labels.index("in-window")
    # wider window pulls the far row in
    wide = [e["label"] for e in client.get("/api/calendar?days=365").json()]
    assert "out-of-window" in wide


# --- manual promotion + document event field ---------------------------------------------

def t1_response(user_text: str) -> EnrichmentT1:
    return EnrichmentT1(
        summary="GST council cuts rates.", event_type="gst_council_decision",
        entities=[T1Entity(surface="GST Council", type="organization"),
                  T1Entity(surface="Finance Ministry", type="ministry")],
        claims=[])


async def test_promote_endpoint_runs_t1_then_t2(env, db):
    client, container = env
    conn = db
    container.enrichment.provider = MockProvider(respond=t1_response)
    doc = (await container.pipeline.ingest_text(
        db,
        "The GST Council cut rates on insurance premiums. The Finance "
        "Ministry said the change applies from July.")).document
    assert client.post("/api/documents/9999/promote").status_code == 404

    res = client.post(f"/api/documents/{doc.id}/promote")
    assert res.status_code == 202
    job_id = res.json()["job_id"]
    job = await wait_for_job(conn, job_id)
    assert job["status"] == "done", job["error"]
    assert await qv(conn, "SELECT kind FROM job WHERE id=%s",
                    job_id) == "enrich_t2"

    # T1 ran, T2 clustered the doc into a new event, tier flipped to 2
    row = await q1(conn, "SELECT enrichment_status, enrichment_tier FROM"
                         " document WHERE id=%s", doc.id)
    assert (row["enrichment_status"], row["enrichment_tier"]) == ("done", 2)
    assignment = await q1(
        conn,
        "SELECT event_id, method FROM event_assignment"
        " WHERE document_id=%s", doc.id)
    assert assignment["method"] == "new"

    # document detail now carries the event ref
    detail = client.get(f"/api/documents/{doc.id}").json()
    assert detail["event"] == {
        "id": assignment["event_id"],
        "title": await qv(conn, "SELECT title FROM event WHERE id=%s",
                          assignment["event_id"])}

    # promoting again is idempotent (already assigned)
    res2 = client.post(f"/api/documents/{doc.id}/promote")
    job2 = await wait_for_job(conn, res2.json()["job_id"])
    assert job2["status"] == "done"
    assert await qv(conn, "SELECT COUNT(*) FROM event_assignment WHERE"
                          " document_id=%s", doc.id) == 1
