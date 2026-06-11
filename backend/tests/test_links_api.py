"""Link surface of the API: document detail links/linked_from + the manual
fetch endpoint — canned fetcher, no network."""

from __future__ import annotations

import pytest

from canned_web import FakeFetcher, html_page

PARENT_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=4242"
OFFICIAL_URL = "https://pib.gov.in/PressReleasePage.aspx?PRID=4242"
CONTENT_URL = "https://example.com/explainers/what-the-circular-means"
DEAD_URL = "https://example.com/explainers/gone-now"


@pytest.fixture()
def web(client):
    """Wire the canned web into the live app's pipeline."""
    fetcher = FakeFetcher({
        PARENT_URL: html_page(
            "Master Direction amendment",
            f'<p>Announced via the <a href="{OFFICIAL_URL}">press '
            f'release</a>; see this <a href="{CONTENT_URL}">explainer on '
            f'the change</a> and an <a href="{DEAD_URL}">archived '
            f'explainer</a> for background.</p>'),
        OFFICIAL_URL: html_page(
            "PIB: Master Direction amended",
            "<p>The ministry detailed the amendment and its phased "
            "implementation timeline for supervised entities.</p>"),
        CONTENT_URL: html_page(
            "Explainer: what the circular means",
            "<p>An analyst walks through the practical impact of the "
            "amendment on smaller cooperative banks and their lending.</p>"),
        # DEAD_URL absent -> manual fetch fails
    })
    client.app.state.container.pipeline.fetcher = fetcher
    return fetcher


def _ingest_parent(client) -> dict:
    resp = client.post("/api/documents", json={"url": PARENT_URL})
    assert resp.status_code == 201
    return resp.json()


def test_document_detail_includes_links_and_linked_from(client, web):
    parent = _ingest_parent(client)

    detail = client.get(f"/api/documents/{parent['id']}").json()
    links = {l["url"]: l for l in detail["links"]}
    assert set(links) == {OFFICIAL_URL, CONTENT_URL, DEAD_URL}

    official = links[OFFICIAL_URL]
    assert official["is_official"] is True
    assert official["status"] == "fetched"  # auto-followed
    assert official["resolved_document_id"] is not None
    assert official["anchor_text"] == "press release"

    # content links (anchor in text, non-official) are stored, not followed
    assert links[CONTENT_URL]["status"] == "not_followed"
    assert links[CONTENT_URL]["is_official"] is False

    # the followed child knows where it was discovered
    child = client.get(
        f"/api/documents/{official['resolved_document_id']}").json()
    assert child["linked_from"] == [
        {"document_id": parent["id"], "title": parent["title"]}]
    assert child["source_id"] is None


def test_manual_fetch_flips_status_and_is_idempotent(client, web):
    parent = _ingest_parent(client)
    detail = client.get(f"/api/documents/{parent['id']}").json()
    link = next(l for l in detail["links"] if l["url"] == CONTENT_URL)
    assert link["status"] == "not_followed"

    resp = client.post(f"/api/document-links/{link['id']}/fetch")
    assert resp.status_code == 200
    body = resp.json()
    assert body["link"]["status"] == "fetched"
    assert body["document"] is not None
    assert body["document"]["id"] == body["link"]["resolved_document_id"]
    assert "cooperative banks" in body["document"]["content_text"]

    calls_after_first = web.calls.count(CONTENT_URL)
    assert calls_after_first == 1

    # idempotent: already fetched -> current state, no re-fetch
    again = client.post(f"/api/document-links/{link['id']}/fetch").json()
    assert again["link"] == body["link"]
    assert again["document"]["id"] == body["document"]["id"]
    assert web.calls.count(CONTENT_URL) == calls_after_first

    # refreshed detail shows it, and the child gained a linked_from
    refreshed = client.get(f"/api/documents/{parent['id']}").json()
    statuses = {l["url"]: l["status"] for l in refreshed["links"]}
    assert statuses[CONTENT_URL] == "fetched"


def test_manual_fetch_failure_recorded(client, web):
    parent = _ingest_parent(client)
    detail = client.get(f"/api/documents/{parent['id']}").json()
    link = next(l for l in detail["links"] if l["url"] == DEAD_URL)

    resp = client.post(f"/api/document-links/{link['id']}/fetch")
    assert resp.status_code == 200
    body = resp.json()
    assert body["link"]["status"] == "failed"
    assert "404" in body["link"]["error"]
    assert body["document"] is None

    # a failed link can be retried (still 200, still failed here)
    retry = client.post(f"/api/document-links/{link['id']}/fetch").json()
    assert retry["link"]["status"] == "failed"


def test_manual_fetch_unknown_id_404(client):
    assert client.post("/api/document-links/999999/fetch").status_code == 404
