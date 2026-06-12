"""POST /documents/{id}/ask — grounded single-document Q&A: the happy answer +
spend ledger row, the ungrounded path, tenancy 404, keyless 503, empty-question
422, and the per-user budget 429. MockProvider only — no network."""

from __future__ import annotations

from mock_llm import MockProvider

from connect.domain.models import DocumentAnswer
from connect.storage.pg import utc_now
from dbutil import qv

GROUNDED = DocumentAnswer(
    answer="The RBI kept the repo rate unchanged at 6.5%.",
    grounded=True, quote="kept the repo rate unchanged at 6.5%")


def _ingest(client, text: str, title: str = "Doc") -> int:
    r = client.post("/api/documents", json={"text": text, "title": title})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _set_llm(client, provider) -> None:
    client.app.state.container.llm = provider


async def test_ask_returns_grounded_answer(client, db):
    doc_id = _ingest(
        client,
        "The RBI kept the repo rate unchanged at 6.5% at its June meeting,"
        " citing easing food inflation.", "RBI repo decision")
    _set_llm(client, MockProvider(respond=GROUNDED))

    r = client.post(f"/api/documents/{doc_id}/ask",
                    json={"question": "What did the RBI do with the repo rate?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grounded"] is True
    assert "6.5%" in body["answer"]
    assert body["quote"]
    # the call is metered under the dedicated document_qa ledger purpose
    assert await qv(db, "SELECT COUNT(*) FROM llm_call"
                        " WHERE purpose = 'document_qa'") == 1


def test_ask_ungrounded_answer(client):
    doc_id = _ingest(client, "An article about monsoon forecasts.", "Weather")
    _set_llm(client, MockProvider(respond=DocumentAnswer(
        answer="The document does not cover the repo rate.",
        grounded=False, quote=None)))
    r = client.post(f"/api/documents/{doc_id}/ask",
                    json={"question": "What is the repo rate?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grounded"] is False and body["quote"] is None


async def test_ask_private_doc_of_another_user_is_404(client, db):
    # a private doc the asker cannot see (owner NULL, not shared) -> 404
    cur = await db.execute(
        "INSERT INTO document (title, fetched_at, media_type, content_text,"
        " content_hash, enrichment_status, visibility, owner_id)"
        " VALUES ('secret', %s, 'text', 'private body', 'h-qa-404',"
        " 'pending', 'private', NULL) RETURNING id", (utc_now(),))
    doc_id = (await cur.fetchone())["id"]
    _set_llm(client, MockProvider(respond=GROUNDED))
    r = client.post(f"/api/documents/{doc_id}/ask", json={"question": "what?"})
    assert r.status_code == 404


def test_ask_keyless_503(client):
    doc_id = _ingest(client, "Some article body text.", "Doc")
    _set_llm(client, None)
    r = client.post(f"/api/documents/{doc_id}/ask", json={"question": "what?"})
    assert r.status_code == 503


def test_ask_empty_question_422(client):
    doc_id = _ingest(client, "body", "Doc")
    _set_llm(client, MockProvider(respond=GROUNDED))
    # whitespace-only passes min_length but the endpoint strips it -> 422
    assert client.post(f"/api/documents/{doc_id}/ask",
                       json={"question": "   "}).status_code == 422
    # missing field -> pydantic 422
    assert client.post(f"/api/documents/{doc_id}/ask",
                       json={}).status_code == 422


async def test_ask_over_budget_429(client, db):
    doc_id = _ingest(client, "Article body for the budget test.", "Doc")
    _set_llm(client, MockProvider(respond=GROUNDED))
    me = client.get("/api/me").json()
    # a prior general-purpose ledger row blows past the daily envelope so the
    # governor refuses BEFORE spending
    await db.execute(
        "INSERT INTO llm_call (purpose, model, input_tokens, output_tokens,"
        " cache_read_tokens, cost_estimate, created_at, user_id)"
        " VALUES ('analysis', 'claude-sonnet-4-6', 1000, 1000, 0, 50.0, %s, %s)",
        (utc_now(), me["id"]))
    r = client.post(f"/api/documents/{doc_id}/ask", json={"question": "what?"})
    assert r.status_code == 429
