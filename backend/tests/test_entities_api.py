"""Entities endpoints (list/detail/documents, co-occurrence with lift),
search with entities, and the document-detail enrichment field — all driven
through the API against data persisted by the real T1 persist path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from connect.api.main import create_app
from connect.knowledge.enrichment import persist
from connect.knowledge.enrichment.t1 import EnrichmentT1, T1Claim, T1Entity


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


def t1(entities, *, topics=("markets",), claims=()):
    return EnrichmentT1(
        summary="Summary.",
        event_type="sebi_action",
        topics=list(topics),
        entities=[T1Entity(surface=s, type=t) for s, t in entities],
        claims=[T1Claim(text=t, check_worthiness=w, quoted_span=q)
                for t, w, q in claims])


@pytest.fixture()
async def corpus(env, db):
    """3 docs: SEBI in all; Adani Group in 2; RBI in 1 -> SEBI+Adani
    co-occur twice (kept, lift 2/2=1.0), SEBI+RBI once (dropped, <2)."""
    client, container = env
    docs = []
    bodies = [
        "SEBI tightened disclosure norms for the Adani Group on Monday. "
        "The securities regulator cited audit gaps in the order.",
        "The Adani Group responded to SEBI saying it will comply fully "
        "with every disclosure requirement raised by the regulator.",
        "SEBI met RBI officials to discuss overlapping custody rules "
        "for foreign portfolio investors in government bonds.",
    ]
    for body in bodies:
        result = await container.pipeline.ingest_text(db, body)
        docs.append(result.document)
    await persist.persist_t1(
        db, document_id=docs[0].id, model="m",
        result=t1([("SEBI", "organization"), ("Adani Group", "company")],
                  topics=("securities-regulation", "markets"),
                  claims=[("SEBI cited audit gaps.", 0.8,
                           "cited audit gaps in the order")]))
    await persist.persist_t1(
        db, document_id=docs[1].id, model="m",
        result=t1([("SEBI", "organization"), ("Adani Group", "company")],
                  topics=("securities-regulation",)))
    await persist.persist_t1(
        db, document_id=docs[2].id, model="m",
        result=t1([("SEBI", "organization"),
                   ("Reserve Bank of India", "organization")],
                  topics=("banking", "markets")))
    return client, container, docs


def _entity_id(client, name):
    items = client.get("/api/entities", params={"q": name}).json()["items"]
    assert items, f"entity {name!r} not found"
    return items[0]["id"]


def test_entities_list_and_query(corpus):
    client, _, _ = corpus
    page = client.get("/api/entities").json()
    assert page["total"] == 3
    assert {"id", "name", "entity_type", "mention_count", "document_count",
            "last_seen_at"} <= set(page["items"][0])
    # mention-count ordering: SEBI (3) first
    assert page["items"][0]["name"] == "SEBI"
    assert page["items"][0]["mention_count"] == 3
    assert page["items"][0]["document_count"] == 3

    # q matches name (case-insensitive)
    found = client.get("/api/entities", params={"q": "adani"}).json()
    assert found["total"] == 1
    assert found["items"][0]["name"] == "Adani Group"

    # q matches alias text too
    found = client.get("/api/entities", params={"q": "Reserve Bank"}).json()
    assert found["total"] == 1

    # pagination
    p2 = client.get("/api/entities",
                    params={"page": 2, "page_size": 2}).json()
    assert p2["total"] == 3 and len(p2["items"]) == 1


def test_entity_detail_with_cooccurrence_lift(corpus):
    client, _, docs = corpus
    sebi = _entity_id(client, "SEBI")
    detail = client.get(f"/api/entities/{sebi}").json()

    assert detail["entity"]["name"] == "SEBI"
    assert detail["entity"]["aliases"] == ["SEBI"]
    assert detail["mention_count"] == 3
    assert detail["document_count"] == 3
    assert detail["first_seen_at"] is not None
    assert detail["last_seen_at"] >= detail["first_seen_at"]

    # topic rollup across SEBI's documents
    topics = {t["topic"]: t["count"] for t in detail["topics"]}
    assert topics["securities-regulation"] == 2
    assert topics["markets"] == 2
    assert topics["banking"] == 1

    # co-occurrence: Adani together=2 kept with lift 2/2=1.0;
    # RBI together=1 dropped (min together >= 2)
    co = detail["co_occurring"]
    assert len(co) == 1
    assert co[0]["entity"]["name"] == "Adani Group"
    assert co[0]["together"] == 2
    assert co[0]["lift"] == 1.0

    # documents: most recent first, DocumentListItem shape
    assert len(detail["documents"]) == 3
    assert {d["id"] for d in detail["documents"]} == {d.id for d in docs}
    assert "enrichment_status" in detail["documents"][0]

    assert client.get("/api/entities/99999").status_code == 404


def test_entity_documents_pagination(corpus):
    client, _, docs = corpus
    sebi = _entity_id(client, "SEBI")
    page = client.get(f"/api/entities/{sebi}/documents",
                      params={"page": 1, "page_size": 2}).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    page2 = client.get(f"/api/entities/{sebi}/documents",
                       params={"page": 2, "page_size": 2}).json()
    assert len(page2["items"]) == 1
    assert client.get("/api/entities/99999/documents").status_code == 404

    adani = _entity_id(client, "Adani")
    page = client.get(f"/api/entities/{adani}/documents").json()
    assert page["total"] == 2


def test_search_with_entities(corpus):
    client, _, _ = corpus
    res = client.get("/api/search",
                     params={"q": "SEBI", "kind": "all"}).json()
    assert res["total_documents"] >= 1
    assert res["total"] == res["total_documents"]  # back-compat field
    assert res["total_entities"] == 1
    assert res["entities"][0]["name"] == "SEBI"
    assert res["documents"][0]["snippet"] is not None

    docs_only = client.get(
        "/api/search", params={"q": "SEBI", "kind": "documents"}).json()
    assert docs_only["entities"] == [] and docs_only["total_entities"] == 0
    assert docs_only["total_documents"] >= 1

    ents_only = client.get(
        "/api/search", params={"q": "SEBI", "kind": "entities"}).json()
    assert ents_only["documents"] == [] and ents_only["total_documents"] == 0
    assert ents_only["total_entities"] == 1

    assert client.get("/api/search",
                      params={"q": "x", "kind": "bogus"}).status_code == 422


def test_document_detail_enrichment_field(corpus):
    client, _, docs = corpus
    body = client.get(f"/api/documents/{docs[0].id}").json()
    enr = body["enrichment"]
    assert enr is not None
    assert enr["summary"] == "Summary."
    assert enr["event_type"] == "sebi_action"
    assert enr["model"] == "m"
    assert enr["topics"] == ["markets", "securities-regulation"]
    assert {e["name"] for e in enr["entities"]} == {"SEBI", "Adani Group"}
    assert len(enr["claims"]) == 1
    assert enr["claims"][0]["check_worthiness"] == 0.8
    assert enr["created_at"]

    # un-enriched document -> enrichment is null
    new_doc = client.post("/api/documents", json={
        "text": "A fresh unenriched document about nothing in particular."})
    assert new_doc.status_code == 201
    body = client.get(f"/api/documents/{new_doc.json()['id']}").json()
    assert body["enrichment"] is None


def test_entity_watch_via_api(corpus):
    """POST /api/watches accepts kind=entity with a real entity id."""
    client, _, _ = corpus
    sebi = _entity_id(client, "SEBI")
    resp = client.post("/api/watches", json={
        "kind": "entity", "entity_id": sebi, "label": "SEBI watch"})
    assert resp.status_code == 201
    watch = resp.json()
    assert watch["kind"] == "entity" and watch["entity_id"] == sebi

    # a new doc mentioning the entity name flips watch_hit
    created = client.post("/api/documents", json={
        "text": "SEBI issued a fresh circular about mutual fund fees."})
    doc_id = created.json()["id"]
    feed = client.get("/api/feed").json()["items"]
    flag = next(d["watch_hit"] for d in feed if d["id"] == doc_id)
    assert flag is True
