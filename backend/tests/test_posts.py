"""Findings board — POST/GET/PATCH/DELETE /api/posts: CRUD lifecycle, the
document link + ?document_id filter, validation, and DAO-level tenancy
(a private post is invisible to other users)."""

from __future__ import annotations

from kb_factories import ensure_user
from mock_llm import MockProvider

from connect.domain.models import DocumentAnswer
from connect.storage import posts as post_dao


def _ingest(client, text: str, title: str = "Source article") -> int:
    r = client.post("/api/documents", json={"text": text, "title": title})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_post_ask_grounded(client):
    """Cross-question a finding: a grounded answer from its cited document."""
    doc = _ingest(client, "The RBI kept the repo rate at 6.5%.", "RBI holds")
    pid = client.post("/api/posts", json={
        "title": "Pause", "body": "A hold.", "document_id": doc}).json()["id"]
    client.app.state.container.llm = MockProvider(respond_by_schema={
        DocumentAnswer: lambda _u: DocumentAnswer(
            answer="The RBI held the repo rate at 6.5%.", grounded=True,
            quote="The RBI kept the repo rate at 6.5%.")})
    a = client.post(f"/api/posts/{pid}/ask",
                    json={"question": "What did the RBI do?"})
    assert a.status_code == 200, a.text
    assert a.json()["grounded"] is True
    assert "RBI" in a.json()["answer"]
    # missing post -> 404
    assert client.post("/api/posts/999999/ask",
                       json={"question": "x"}).status_code == 404


def test_post_crud_lifecycle(client):
    # create
    r = client.post("/api/posts", json={
        "title": "RBI likely to hold in August",
        "body": "Across three outlets the framing shifted to a pause."})
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["title"].startswith("RBI likely")
    assert post["visibility"] == "shared"
    assert post["owner_id"] and post["owner_name"]
    pid = post["id"]

    # list shows it
    listed = client.get("/api/posts").json()
    assert any(p["id"] == pid for p in listed)

    # get one
    assert client.get(f"/api/posts/{pid}").json()["id"] == pid

    # patch body + visibility
    r = client.patch(f"/api/posts/{pid}",
                     json={"body": "Revised: two outlets, not three.",
                           "visibility": "private"})
    assert r.status_code == 200
    assert r.json()["visibility"] == "private"
    assert "Revised" in r.json()["body"]
    assert r.json()["updated_at"] is not None

    # delete -> 204, then 404
    assert client.delete(f"/api/posts/{pid}").status_code == 204
    assert client.get(f"/api/posts/{pid}").status_code == 404


def test_post_linked_to_document(client):
    doc_id = _ingest(client, "The RBI kept the repo rate unchanged at 6.5%.",
                     "RBI repo decision")
    r = client.post("/api/posts", json={
        "title": "Repo held — my read",
        "body": "Signals a longer pause than the market expects.",
        "document_id": doc_id})
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["document_id"] == doc_id
    assert post["document_title"] == "RBI repo decision"   # joined in

    # the ?document_id filter returns findings posted from that article
    filtered = client.get("/api/posts", params={"document_id": doc_id}).json()
    assert [p["id"] for p in filtered] == [post["id"]]


def test_post_validation(client):
    # empty title/body -> 422
    assert client.post("/api/posts",
                       json={"title": "  ", "body": "x"}).status_code == 422
    assert client.post("/api/posts", json={"title": "t"}).status_code == 422
    # link to a non-existent document -> 422
    assert client.post("/api/posts", json={
        "title": "t", "body": "b", "document_id": 999999}).status_code == 422


async def test_private_post_invisible_to_other_users(client, db):
    """DAO-level tenancy: a private post is visible only to its owner; a
    shared one is visible to everyone."""
    owner = await ensure_user(db, email="poster@test.local")
    other = await ensure_user(db, email="stranger@test.local")

    private = await post_dao.insert(
        db, owner_id=owner, title="private note", body="secret read",
        visibility="private")
    shared = await post_dao.insert(
        db, owner_id=owner, title="shared note", body="public read",
        visibility="shared")

    # owner sees both; other user sees only the shared one
    assert await post_dao.get(db, private.id, viewer=owner) is not None
    assert await post_dao.get(db, private.id, viewer=other) is None
    assert await post_dao.get(db, shared.id, viewer=other) is not None

    other_ids = {p.id for p in await post_dao.list_visible(db, viewer=other)}
    assert shared.id in other_ids and private.id not in other_ids
