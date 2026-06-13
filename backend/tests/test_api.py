"""API smoke tests via TestClient — exercises the full contract surface."""

from __future__ import annotations

TEXT = (
    "The Reserve Bank of India kept the repo rate unchanged at its June "
    "monetary policy committee meeting, citing easing food inflation and a "
    "stable rupee. The governor said the stance remains withdrawal of "
    "accommodation while liquidity stays in surplus.")


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["schema_version"] == 15  # PG lineage (v15: story mode)
    assert body["vector_backend"] == "disabled"  # embeddings off in tests
    assert body["db_path"].startswith("postgresql://")
    # the DSN's password must never leak through the unauthenticated
    # health endpoint — it is redacted to *** (storage.pg.redact_dsn)
    assert ":connect@" not in body["db_path"]
    assert ":***@" in body["db_path"]


def test_redact_dsn():
    from connect.storage.pg import redact_dsn

    assert (redact_dsn("postgresql://u:secret@h:5/db")
            == "postgresql://u:***@h:5/db")
    # url-encoded / odd passwords, and passwordless DSNs stay intact
    assert (redact_dsn("postgresql://u:p%40ss@h/db")
            == "postgresql://u:***@h/db")
    assert redact_dsn("postgresql://h:5432/db") == "postgresql://h:5432/db"
    assert (redact_dsn("host=h password=secret dbname=db")
            == "host=h password=*** dbname=db")


def test_seeded_sources_present(client):
    sources = client.get("/api/sources").json()
    names = {s["name"] for s in sources}
    assert "RBI Notifications" in names
    assert "Google News India" in names
    google = next(s for s in sources if s["name"] == "Google News India")
    assert google["t1_exempt"] is True
    assert google["credibility_tier"] == 3
    rbi = next(s for s in sources if s["name"] == "RBI Notifications")
    assert rbi["config"]["feed_url"] == "https://rbi.org.in/notifications_rss.xml"
    assert rbi["doc_count"] == 0


def test_source_crud_and_validation(client):
    # invalid rss config rejected at registration
    resp = client.post("/api/sources", json={
        "name": "bad", "type": "rss", "config": {}, "credibility_tier": 2})
    assert resp.status_code == 422

    resp = client.post("/api/sources", json={
        "name": "My Paste Bucket", "type": "manual", "config": {},
        "credibility_tier": 4, "notes": "hand ingests"})
    assert resp.status_code == 201
    source = resp.json()
    assert source["type"] == "manual"

    # duplicate name -> 409
    resp = client.post("/api/sources", json={
        "name": "My Paste Bucket", "type": "manual", "config": {},
        "credibility_tier": 4})
    assert resp.status_code == 409

    resp = client.patch(f"/api/sources/{source['id']}",
                        json={"credibility_tier": 3, "enabled": False})
    assert resp.status_code == 200
    assert resp.json()["credibility_tier"] == 3
    assert resp.json()["enabled"] is False

    assert client.delete(f"/api/sources/{source['id']}").status_code == 204
    assert client.get(f"/api/sources/{source['id']}").status_code == 404


def test_source_test_endpoint_manual_and_stub(client):
    resp = client.post("/api/sources/test",
                       json={"type": "manual", "config": {}})
    assert resp.json() == {"ok": True, "sample_items": [], "error": None}

    resp = client.post("/api/sources/test",
                       json={"type": "search", "config": {}})
    body = resp.json()
    assert body["ok"] is False and body["error"]


def test_poll_non_rss_source_rejected(client):
    resp = client.post("/api/sources", json={
        "name": "uploads", "type": "manual", "config": {},
        "credibility_tier": 4})
    sid = resp.json()["id"]
    assert client.post(f"/api/sources/{sid}/poll").status_code == 400


def test_ingest_search_feed_flow(client):
    # ingest text
    resp = client.post("/api/documents",
                       json={"text": TEXT, "title": "RBI holds repo rate"})
    assert resp.status_code == 201
    doc = resp.json()
    assert doc["title"] == "RBI holds repo rate"
    assert doc["enrichment_status"] == "pending"

    # exact dedup -> 200 + existing document
    resp = client.post("/api/documents", json={"text": TEXT})
    assert resp.status_code == 200
    assert resp.json()["id"] == doc["id"]

    # detail includes content_text + hash
    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert "repo rate" in detail["content_text"]
    assert detail["content_hash"]

    # search finds it (with snippet)
    result = client.get("/api/search", params={"q": "repo rate"}).json()
    assert result["total"] == 1
    assert result["documents"][0]["id"] == doc["id"]
    assert result["documents"][0]["snippet"]

    # documents listing with q= uses FTS too
    page = client.get("/api/documents", params={"q": "inflation"}).json()
    assert page["total"] == 1 and page["page"] == 1

    # feed lists it newest-first with status filter
    feed = client.get("/api/feed").json()
    assert feed["total"] == 1
    assert feed["items"][0]["id"] == doc["id"]
    assert client.get("/api/feed",
                      params={"status": "skipped_dup"}).json()["total"] == 0
    assert client.get("/api/feed",
                      params={"status": "bogus"}).status_code == 422


def test_ingest_validation_errors(client):
    assert client.post("/api/documents", json={}).status_code == 422
    assert client.post("/api/documents",
                       json={"text": "   "}).status_code == 422


def test_ingest_file_upload(client):
    resp = client.post(
        "/api/documents",
        files={"file": ("note.txt", b"Cabinet approves semiconductor "
                                    b"incentive scheme phase two",
                        "text/plain")})
    assert resp.status_code == 201
    body = resp.json()
    assert body["media_type"] == "text"
    found = client.get("/api/search", params={"q": "semiconductor"}).json()
    assert found["total"] == 1


def test_watch_lifecycle_and_badges(client):
    # validation: topic watch needs query_fts
    assert client.post("/api/watches", json={
        "kind": "topic", "label": "x"}).status_code == 422

    resp = client.post("/api/watches", json={
        "kind": "topic", "label": "monetary policy",
        "query_fts": '"repo rate"'})
    assert resp.status_code == 201
    watch = resp.json()
    assert watch["promote"] is True and watch["muted"] is False

    # matching document ingested AFTER the watch -> hit + badge
    resp = client.post("/api/documents",
                       json={"text": TEXT, "title": "RBI holds repo rate"})
    doc = resp.json()
    assert doc["watch_hit"] is True

    badges = client.get("/api/watches/badges").json()
    assert badges == {str(watch["id"]): 1}

    # seen resets the cursor
    seen = client.post(f"/api/watches/{watch['id']}/seen")
    assert seen.status_code == 200
    assert seen.json()["last_seen_at"] is not None
    assert client.get("/api/watches/badges").json() == {str(watch["id"]): 0}

    # patch + delete
    resp = client.patch(f"/api/watches/{watch['id']}", json={"muted": True})
    assert resp.json()["muted"] is True
    assert client.get("/api/watches/badges").json() == {}
    assert client.delete(f"/api/watches/{watch['id']}").status_code == 204
    assert client.get("/api/watches").json() == []
