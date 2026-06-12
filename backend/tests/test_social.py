"""Social posting — POST /social/documents/{id}/draft generates a grounded
caption + a rendered JPEG card; /instagram/status + /publish gate on
connection; the public /social/card/{sha}.jpg serves only from the dedicated
card store (never document blobs). MockProvider only — no network."""

from __future__ import annotations

import base64
import io

from mock_llm import MockProvider
from PIL import Image

from connect.domain.models import SocialPost
from connect.social.card import render_card
from connect.storage.pg import utc_now
from dbutil import qv

SAMPLE = SocialPost(
    headline="RBI holds repo rate at 6.5%",
    caption="The RBI kept the repo rate unchanged at its June meeting. Source: RBI.",
    hashtags=["RBI", "RepoRate", "IndianEconomy"],
    key_points=["Repo rate unchanged at 6.5%", "Food inflation easing"],
    source_label="Source: RBI",
    alt_text="A card summarizing the RBI repo decision")


def _ingest(client, text: str, title: str = "Doc") -> int:
    r = client.post("/api/documents", json={"text": text, "title": title})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _set_llm(client, provider) -> None:
    client.app.state.container.llm = provider


async def test_draft_returns_content_and_card(client, db):
    doc_id = _ingest(
        client,
        "The Reserve Bank of India kept the repo rate unchanged at 6.5% at its"
        " June meeting, citing easing food inflation.", "RBI repo decision")
    _set_llm(client, MockProvider(respond=SAMPLE))

    r = client.post(f"/api/social/documents/{doc_id}/draft")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"]["headline"].startswith("RBI holds")
    assert "RBI" in body["content"]["hashtags"]
    # the embedded card is a real 1080x1080 JPEG
    img = Image.open(io.BytesIO(base64.b64decode(body["image_b64"])))
    assert img.format == "JPEG" and img.size == (1080, 1080)
    # metered under the document-qa-sibling 'social_post' purpose
    assert await qv(db, "SELECT COUNT(*) FROM llm_call"
                        " WHERE purpose = 'social_post'") == 1


async def test_draft_private_doc_of_another_user_is_404(client, db):
    cur = await db.execute(
        "INSERT INTO document (title, fetched_at, media_type, content_text,"
        " content_hash, enrichment_status, visibility, owner_id)"
        " VALUES ('secret', %s, 'text', 'private body', 'h-social-404',"
        " 'pending', 'private', NULL) RETURNING id", (utc_now(),))
    doc_id = (await cur.fetchone())["id"]
    _set_llm(client, MockProvider(respond=SAMPLE))
    assert client.post(
        f"/api/social/documents/{doc_id}/draft").status_code == 404


def test_draft_keyless_503(client):
    doc_id = _ingest(client, "Some article body text.", "Doc")
    _set_llm(client, None)
    assert client.post(
        f"/api/social/documents/{doc_id}/draft").status_code == 503


def test_instagram_status_disconnected_by_default(client):
    # the test settings configure no Instagram tokens
    body = client.get("/api/social/instagram/status").json()
    assert body["connected"] is False and body["account_id"] is None


def test_publish_409_when_not_connected(client):
    doc_id = _ingest(client, "Article body.", "Doc")
    r = client.post(f"/api/social/documents/{doc_id}/publish",
                    json={"content": SAMPLE.model_dump()})
    assert r.status_code == 409


def test_public_card_endpoint_serves_card(anon_client, client):
    # store a card directly, then fetch it WITHOUT auth (Instagram fetches it)
    card = render_card(SAMPLE)
    rel = client.app.state.container.card_store.put(card)
    sha = rel.rsplit("/", 1)[-1]
    r = anon_client.get(f"/api/social/card/{sha}.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == card


def test_public_card_endpoint_does_not_serve_document_blobs(client):
    # a blob in the DOCUMENT store must not be reachable via the card endpoint
    doc_blob_sha = client.app.state.container.blobs.put(b"raw private doc bytes")
    sha = doc_blob_sha.rsplit("/", 1)[-1]
    assert client.get(f"/api/social/card/{sha}.jpg").status_code == 404
    # garbage sha -> 404, never a path traversal
    assert client.get("/api/social/card/not-a-sha.jpg").status_code == 404
