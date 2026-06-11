"""Today brief: lazy generation, same-day idempotence, the five-section
shape, watch_dev + thread_move content (template reasons + frozen
payloads), the seen flag, and past-date behavior. Zero LLM anywhere."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from kb_factories import insert_doc, insert_source, t1_doc

from connect.api.main import create_app
from connect.llm.spend import today_utc
from connect.storage.pg import utc_now

ALL_SECTIONS = ("watch_dev", "thread_move", "contradiction",
                "trending_claim", "suggestion", "position_shift")


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


async def add_watch(conn, label, query, *, last_seen_at=None):
    cur = await conn.execute(
        "INSERT INTO watch (kind, label, query_fts, last_seen_at,"
        " created_at) VALUES ('topic', %s, %s, %s, %s) RETURNING id",
        (label, query, last_seen_at, utc_now()))
    return int((await cur.fetchone())["id"])


async def add_hit(conn, watch_id, doc_id, *, created_at=None):
    await conn.execute(
        "INSERT INTO watch_hit (watch_id, object_type, object_id,"
        " created_at) VALUES (%s, 'document', %s, %s)",
        (watch_id, doc_id, created_at or utc_now()))


async def make_story_with_new_event(conn, *, title="Bill story", tier=1):
    src = await insert_source(conn, f"src-{title}", tier=tier)
    doc = await t1_doc(conn, title="Bill passed", source_id=src,
                       event_type="bill_stage")
    now = utc_now()
    cur = await conn.execute(
        "INSERT INTO story (title, status, doc_count, created_at,"
        " updated_at) VALUES (%s, 'active', 1, %s, %s) RETURNING id",
        (title, now, now))
    story_id = int((await cur.fetchone())["id"])
    cur = await conn.execute(
        "INSERT INTO event (title, event_type, story_id, occurred_on,"
        " doc_count, created_at) VALUES ('Bill passed', 'bill_stage',"
        " %s, %s, 1, %s) RETURNING id", (story_id, now[:10], now))
    event_id = int((await cur.fetchone())["id"])
    await conn.execute(
        "INSERT INTO event_assignment (document_id, event_id, method,"
        " created_at) VALUES (%s, %s, 'new', %s)", (doc, event_id, now))
    return story_id


# --- shape + idempotence ------------------------------------------------------------

def test_empty_brief_has_all_five_sections(env):
    client, _ = env
    res = client.get("/api/brief/today")
    assert res.status_code == 200
    body = res.json()
    assert body["brief"]["brief_date"] == today_utc()
    assert set(body["sections"].keys()) == set(ALL_SECTIONS)
    assert all(body["sections"][s] == [] for s in ALL_SECTIONS)


async def test_brief_is_generated_once_and_stable(env, db):
    client, container = env
    first = client.get("/api/brief/today").json()
    # data arriving AFTER generation does not change today's brief
    w = await add_watch(db, "SEBI", "sebi")
    await add_hit(db, w, await insert_doc(db, title="late doc"))
    second = client.get("/api/brief/today").json()
    assert second["brief"]["id"] == first["brief"]["id"]
    assert second["brief"]["generated_at"] == first["brief"]["generated_at"]
    assert second["sections"]["watch_dev"] == []
    # the dated route serves the same persisted brief
    dated = client.get(f"/api/brief/{today_utc()}").json()
    assert dated["brief"]["id"] == first["brief"]["id"]


def test_past_date_returns_404_not_a_backfill(env):
    client, _ = env
    assert client.get("/api/brief/1999-01-02").status_code == 404
    assert client.get("/api/brief/not-a-date").status_code == 422


# --- watch_dev -------------------------------------------------------------------------

async def test_watch_dev_items(env, db):
    client, container = env
    conn = db
    src = await insert_source(conn, "ET Markets", tier=2)
    w = await add_watch(conn, "SEBI", "sebi")
    d1 = await insert_doc(conn, title="SEBI tightens norms", source_id=src,
                          published_at="2026-06-10T05:00:00Z")
    d2 = await insert_doc(conn, title="SEBI order on brokers",
                          source_id=src)
    await add_hit(conn, w, d1)
    await add_hit(conn, w, d2)
    # a muted watch's hits never reach the brief
    muted = await add_watch(conn, "Muted", "muted")
    muted_doc = await insert_doc(conn, title="muted hit")
    await add_hit(conn, muted, muted_doc)
    await conn.execute("UPDATE watch SET muted=TRUE WHERE id=%s", (muted,))
    body = client.get("/api/brief/today").json()
    items = body["sections"]["watch_dev"]
    assert len(items) == 2
    assert [i["rank"] for i in items] == [1, 2]
    assert all(i["object_type"] == "document" for i in items)
    assert {i["object_id"] for i in items} == {d1, d2}
    assert all("SEBI" in i["reason"] and "2 new" in i["reason"]
               for i in items)
    by_id = {i["object_id"]: i for i in items}
    assert by_id[d1]["payload"]["title"] == "SEBI tightens norms"
    assert by_id[d1]["payload"]["source"] == "ET Markets"
    assert by_id[d1]["payload"]["watch_label"] == "SEBI"
    assert all(i["seen"] is False for i in items)
    assert muted_doc not in by_id


async def test_watch_dev_respects_read_cursor(env, db):
    client, container = env
    conn = db
    w = await add_watch(conn, "GST", "gst", last_seen_at=utc_now())
    old_doc = await insert_doc(conn, title="old hit")
    await add_hit(conn, w, old_doc, created_at="2026-01-01T00:00:00.000Z")
    body = client.get("/api/brief/today").json()
    assert body["sections"]["watch_dev"] == []


# --- thread_move -----------------------------------------------------------------------

async def test_thread_move_items_with_heat(env, db):
    client, container = env
    conn = db
    story_id = await make_story_with_new_event(conn, title="Bill story",
                                               tier=1)
    body = client.get("/api/brief/today").json()
    items = body["sections"]["thread_move"]
    assert len(items) == 1
    item = items[0]
    assert item["object_type"] == "thread"
    assert item["object_id"] == story_id
    assert item["reason"] == "+1 new event · 1 tier-1/2 source"
    assert item["payload"]["title"] == "Bill story"
    assert item["payload"]["heat"] == 1
    assert item["payload"]["new_events"] == 1


async def test_thread_move_ignores_old_events(env, db):
    client, container = env
    conn = db
    story_id = await make_story_with_new_event(conn)
    await conn.execute(
        "UPDATE event SET created_at='2026-01-01T00:00:00.000Z'"
        " WHERE story_id=%s", (story_id,))
    body = client.get("/api/brief/today").json()
    assert body["sections"]["thread_move"] == []


# --- seen flag -------------------------------------------------------------------------

async def test_mark_item_seen(env, db):
    client, container = env
    conn = db
    w = await add_watch(conn, "SEBI", "sebi")
    await add_hit(conn, w, await insert_doc(conn, title="SEBI doc"))
    body = client.get("/api/brief/today").json()
    item_id = body["sections"]["watch_dev"][0]["id"]

    res = client.post(f"/api/brief/items/{item_id}/seen")
    assert res.status_code == 204
    again = client.get("/api/brief/today").json()
    assert again["sections"]["watch_dev"][0]["seen"] is True
    assert client.post("/api/brief/items/99999/seen").status_code == 404
