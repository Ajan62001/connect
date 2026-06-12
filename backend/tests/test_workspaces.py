"""Workspaces — CRUD lifecycle, tenancy, the focused feed (source / topic /
FTS lensing), and findings + watches tagged and filtered by workspace."""

from __future__ import annotations

from kb_factories import ensure_user, insert_doc, insert_source

from connect.storage import workspaces as workspace_dao
from connect.storage.pg import utc_now


def test_workspace_crud_lifecycle(client):
    r = client.post("/api/workspaces", json={
        "name": "RBI & Monetary Policy",
        "description": "Everything rates",
        "topics": ["monetary-policy", "banking"],
        "query_fts": "repo rate"})
    assert r.status_code == 201, r.text
    ws = r.json()
    assert ws["name"] == "RBI & Monetary Policy"
    assert ws["topics"] == ["monetary-policy", "banking"]
    assert ws["owner_id"] and ws["owner_name"]
    wid = ws["id"]

    assert any(w["id"] == wid for w in client.get("/api/workspaces").json())
    assert client.get(f"/api/workspaces/{wid}").json()["id"] == wid

    r = client.patch(f"/api/workspaces/{wid}",
                     json={"query_fts": "repo rate OR inflation",
                           "visibility": "private"})
    assert r.status_code == 200
    assert r.json()["visibility"] == "private"
    assert r.json()["query_fts"] == "repo rate OR inflation"
    assert r.json()["updated_at"] is not None

    assert client.delete(f"/api/workspaces/{wid}").status_code == 204
    assert client.get(f"/api/workspaces/{wid}").status_code == 404


async def test_private_workspace_invisible_to_others(client, db):
    owner = await ensure_user(db, email="wsowner@test.local")
    other = await ensure_user(db, email="wsother@test.local")
    private = await workspace_dao.insert(
        db, owner_id=owner, name="secret", visibility="private")
    shared = await workspace_dao.insert(
        db, owner_id=owner, name="open", visibility="shared")

    assert await workspace_dao.get(db, private.id, viewer=other) is None
    assert await workspace_dao.get(db, shared.id, viewer=other) is not None
    other_ids = {w.id for w in await workspace_dao.list_visible(db, viewer=other)}
    assert shared.id in other_ids and private.id not in other_ids


async def test_focused_feed_filters_by_source_topic_and_query(client, db):
    # three docs: one from a focus source, one tagged a focus topic, one off-focus
    src = await insert_source(db, "RBI", tier=1)
    other_src = await insert_source(db, "Random", tier=3)
    on_source = await insert_doc(db, title="RBI circular", source_id=src,
                                 text="circular text")
    on_topic = await insert_doc(db, title="Budget explainer",
                                source_id=other_src, text="budget text")
    await db.execute(
        "INSERT INTO document_topic (document_id, topic, source)"
        " VALUES (%s, 'taxation', 't1')", (on_topic,))
    on_query = await insert_doc(db, title="Repo rate cut likely",
                                source_id=other_src,
                                text="the repo rate may be cut")
    off = await insert_doc(db, title="Cricket scores", source_id=other_src,
                           text="unrelated sports content")

    me = client.get("/api/me").json()
    ws = await workspace_dao.insert(
        db, owner_id=me["id"], name="Focus",
        topics=["taxation"], source_ids=[src], query_fts="repo rate")

    feed = client.get(f"/api/workspaces/{ws.id}/feed").json()
    ids = {item["id"] for item in feed["items"]}
    assert {on_source, on_topic, on_query} <= ids
    assert off not in ids


def test_finding_tagged_and_filtered_by_workspace(client):
    wid = client.post("/api/workspaces",
                      json={"name": "WS"}).json()["id"]
    # a finding posted into the workspace
    p1 = client.post("/api/posts", json={
        "title": "in workspace", "body": "x", "workspace_id": wid}).json()
    assert p1["workspace_id"] == wid
    # a finding with no workspace
    client.post("/api/posts", json={"title": "loose", "body": "y"})

    scoped = client.get("/api/posts", params={"workspace_id": wid}).json()
    assert [p["id"] for p in scoped] == [p1["id"]]


def test_add_text_document_to_workspace_kb(client):
    # a focused workspace; the added doc is OUTSIDE the focus (about RBI)
    wid = client.post("/api/workspaces",
                      json={"name": "KB", "query_fts": "tariffs"}).json()["id"]
    r = client.post(f"/api/workspaces/{wid}/documents", json={
        "text": "An internal note about the RBI repo decision.",
        "title": "My KB note"})
    assert r.status_code == 201, r.text

    # it lists in the workspace's knowledge base
    kb = client.get(f"/api/workspaces/{wid}/documents").json()
    assert kb["total"] == 1 and kb["items"][0]["title"] == "My KB note"
    # and it shows in the focused feed even though it's outside the lens
    feed = client.get(f"/api/workspaces/{wid}/feed").json()
    assert any(i["title"] == "My KB note" for i in feed["items"])
    # a different workspace does NOT see it in its KB
    other = client.post("/api/workspaces", json={"name": "Other"}).json()["id"]
    assert client.get(f"/api/workspaces/{other}/documents").json()["total"] == 0


def test_watch_tagged_and_filtered_by_workspace(client):
    wid = client.post("/api/workspaces",
                      json={"name": "WS2"}).json()["id"]
    w1 = client.post("/api/watches", json={
        "kind": "topic", "label": "rates", "query_fts": "repo",
        "workspace_id": wid}).json()
    assert w1["workspace_id"] == wid
    client.post("/api/watches", json={
        "kind": "topic", "label": "loose", "query_fts": "x"})

    scoped = client.get("/api/watches", params={"workspace_id": wid}).json()
    assert [w["id"] for w in scoped] == [w1["id"]]
