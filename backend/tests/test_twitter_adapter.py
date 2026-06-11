"""Twitter adapter (twitterapi.io): payload fixture -> items/documents,
single batched query, pagination rule, key-missing no-op, cursor advance —
all over httpx.MockTransport / canned fetchers, zero network."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from canned_web import FakeFetcher, html_page
from connect.ingestion.poller import SourcePoller
from connect.sources.adapters.twitter import (
    API_URL,
    PAGE_SIZE,
    TwitterAdapter,
    build_query,
    parse_search_page,
    since_epoch,
)
from connect.sources.base import AdapterSkip
from connect.storage import documents as doc_dao
from connect.storage import links as link_dao
from connect.storage import sources as source_dao

FIXTURE = Path(__file__).parent / "fixtures" / "twitter_search.json"
PIB_PRESS_URL = "https://pib.gov.in/PressReleasePage.aspx?PRID=2026100"


def _fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text())


def _adapter(handler, api_key: str | None = "test-key") -> TwitterAdapter:
    transport = httpx.MockTransport(handler)
    return TwitterAdapter(
        api_key=api_key, client=httpx.AsyncClient(transport=transport))


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(
        iso.replace("Z", "+00:00")).timestamp())


# --- pure parsing -----------------------------------------------------------------


def test_parse_search_page_fixture():
    items = parse_search_page(_fixture_payload())

    # the empty-text tweet is skipped
    assert len(items) == 2

    first = items[0]
    assert first.url == "https://x.com/PIB_India/status/1932700000000000001"
    assert first.author == "@PIB_India"
    assert first.media_type == "tweet"
    assert first.published_at == "2026-06-10T09:15:00Z"
    # title: '@<handle>: <first 80 chars>'
    assert first.title.startswith("@PIB_India: Cabinet approves")
    assert len(first.title) == len("@PIB_India: ") + 80
    # expanded urls only — never t.co
    assert first.link_urls == (PIB_PRESS_URL,)
    # raw blob is the tweet JSON (provider-swap insurance)
    assert json.loads(first.raw)["id"] == "1932700000000000001"

    second = items[1]
    assert second.url == (
        "https://x.com/PIBFactCheck/status/1932700000000000002")
    # quoted tweet text is appended
    assert "\n\n[quoting @viralhandle]: Breaking: UPI transactions" \
        in second.content_text
    assert second.content_text.startswith("A viral message claims")
    assert second.link_urls == ()


def test_build_query_is_or_joined_with_cursor():
    q = build_query(["PMOIndia", "PIB_India", "RBI"], 1781000000)
    assert q == ("(from:PMOIndia OR from:PIB_India OR from:RBI)"
                 " since_time:1781000000")


def test_since_epoch_from_iso_and_first_poll_lookback():
    assert since_epoch("2026-06-10T12:00:00Z") == _epoch("2026-06-10T12:00:00Z")
    # first poll (or junk cursor): ~24h lookback
    now = datetime.now(timezone.utc).timestamp()
    for value in (None, "not-a-timestamp"):
        lookback = now - since_epoch(value)
        assert 23.9 * 3600 < lookback < 24.1 * 3600


# --- batched call discipline ---------------------------------------------------------


async def test_discover_makes_one_batched_call():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_fixture_payload())

    adapter = _adapter(handler)
    items = await adapter.discover(
        {"handles": ["PIB_India", "PIBFactCheck", "RBI"]},
        since="2026-06-10T00:00:00Z")

    assert len(requests) == 1  # ONE call for all handles, no per-handle fetches
    req = requests[0]
    assert str(req.url).startswith(API_URL)
    assert req.headers["X-API-Key"] == "test-key"
    params = req.url.params
    assert params["queryType"] == "Latest"
    assert params["cursor"] == ""
    assert params["query"] == (
        "(from:PIB_India OR from:PIBFactCheck OR from:RBI)"
        f" since_time:{_epoch('2026-06-10T00:00:00Z')}")
    assert len(items) == 2


async def test_discover_paginates_only_on_full_page_with_token():
    def tweet(i: int) -> dict:
        return {"id": str(1000 + i), "text": f"tweet number {i} body",
                "createdAt": "Wed Jun 10 09:15:00 +0000 2026",
                "author": {"userName": "PIB_India"},
                "entities": {"urls": []}}

    cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params["cursor"]
        cursors.append(cursor)
        if cursor == "":
            return httpx.Response(200, json={
                "tweets": [tweet(i) for i in range(PAGE_SIZE)],
                "has_next_page": True, "next_cursor": "CUR1"})
        return httpx.Response(200, json={
            "tweets": [tweet(100 + i) for i in range(3)],
            "has_next_page": False, "next_cursor": ""})

    items = await _adapter(handler).discover({"handles": ["PIB_India"]})
    assert cursors == ["", "CUR1"]
    assert len(items) == PAGE_SIZE + 3


async def test_discover_does_not_paginate_short_page_even_with_token():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # ad-filtered short page that still claims a next page: stop anyway
        return httpx.Response(200, json={
            "tweets": _fixture_payload()["tweets"],
            "has_next_page": True, "next_cursor": "CUR1"})

    await _adapter(handler).discover({"handles": ["PIB_India"]})
    assert calls == 1


# --- key-missing no-op ----------------------------------------------------------------


async def test_discover_without_key_raises_adapter_skip():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no HTTP call may happen without a key")

    adapter = _adapter(handler, api_key=None)
    with pytest.raises(AdapterSkip, match="TWITTERAPI_IO_API_KEY not set"):
        await adapter.discover({"handles": ["PIB_India"]})
    with pytest.raises(AdapterSkip):
        await adapter.sample({"handles": ["PIB_India"]})


async def test_poll_without_key_records_clean_skip_status(container, db):
    source = await source_dao.insert(
        db, name="X gov test", type_="twitter",
        config={"handles": ["PIB_India"]}, credibility_tier=1)
    poller = SourcePoller(
        container.pool, pipeline=container.pipeline,
        adapters={"twitter": TwitterAdapter(api_key=None)})

    status = await poller.poll_source(db, source)
    assert status == "skipped: TWITTERAPI_IO_API_KEY not set"
    stored = await source_dao.get(db, source.id)
    assert stored.last_poll_status == "skipped: TWITTERAPI_IO_API_KEY not set"
    assert stored.last_polled_at is not None
    assert await doc_dao.url_exists(
        db, "https://x.com/PIB_India/status/1932700000000000001") is False


# --- end to end through the poller + pipeline ------------------------------------------


async def test_poll_twitter_source_end_to_end(container, db):
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["query"])
        return httpx.Response(200, json=_fixture_payload())

    adapter = _adapter(handler)
    # the expanded official link gets auto-followed through the pipeline
    container.pipeline.fetcher = FakeFetcher({
        PIB_PRESS_URL: html_page(
            "Cabinet approves semiconductor mission",
            "<p>The Union Cabinet approved phase two of the mission with "
            "an outlay of seventy six thousand crore rupees.</p>"),
    })
    source = await source_dao.insert(
        db, name="X gov e2e", type_="twitter",
        config={"handles": ["PIB_India", "PIBFactCheck"]},
        credibility_tier=1)
    poller = SourcePoller(container.pool, pipeline=container.pipeline,
                          adapters={"twitter": adapter})

    status = await poller.poll_source(db, source)
    assert status == "ok: 2 new, 0 dup, 0 error"

    doc = await doc_dao.get_by_url(
        db, "https://x.com/PIB_India/status/1932700000000000001")
    assert doc is not None
    assert doc.media_type == "tweet"
    assert doc.author == "@PIB_India"
    assert doc.source_id == source.id
    assert doc.published_at == "2026-06-10T09:15:00.000Z"  # hint wins
    assert doc.title.startswith("@PIB_India: Cabinet approves")

    # raw blob is the tweet JSON
    from dbutil import qv
    blob_path = await qv(db, "SELECT raw_blob_path FROM document"
                             " WHERE id=%s", doc.id)
    raw = container.blobs.get(blob_path)
    assert json.loads(raw)["id"] == "1932700000000000001"

    # expanded url -> document_link row, classified official + auto-followed
    links = await link_dao.list_for_document(db, doc.id)
    assert [l.url for l in links] == [PIB_PRESS_URL]
    assert links[0].is_official and not links[0].is_file
    assert links[0].status == "fetched"
    assert links[0].resolved_document_id is not None

    # quoted tweet text was appended on the second document
    doc2 = await doc_dao.get_by_url(
        db,
        "https://x.com/PIBFactCheck/status/1932700000000000002")
    assert doc2 is not None
    assert "[quoting @viralhandle]:" in doc2.content_text
    assert await link_dao.list_for_document(db, doc2.id) == []

    # second poll: cursor advanced to last_polled_at, tweet ids are
    # idempotent via URL dedup
    polled = await source_dao.get(db, source.id)
    status = await poller.poll_source(db, polled)
    assert status == "ok: 0 new, 2 dup, 0 error"
    assert len(queries) == 2
    assert queries[1].endswith(
        f"since_time:{_epoch(polled.last_polled_at)}")
    assert (await doc_dao.get_by_url(db, doc.url)).id == doc.id
