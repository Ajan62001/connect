"""SearchClient backends: Tavily request/response shape, DuckDuckGo html
parsing (pure, fixture-driven), the keyless-default factory, and both
clients over a mocked transport — zero live network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from connect.retrieval.search_client import (
    DuckDuckGoSearchClient,
    NullSearchClient,
    SearchError,
    TavilyClient,
    _ddg_unwrap,
    create_search_client,
    parse_ddg_html,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ddg_results.html"

RBI = "https://www.rbi.org.in/pressrelease.aspx?id=12345"
LIVEMINT = "https://www.livemint.com/economy/rbi-mpc-outcome-9876543.html"
ET = "https://economictimes.indiatimes.com/news/economy/policy/article-1.cms"


# --- Tavily ------------------------------------------------------------------

def mock_tavily(handler) -> TavilyClient:
    client = TavilyClient("tvly-test-key")
    # swap the transport, keep the real headers/config
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer tvly-test-key"})
    return client


async def test_tavily_request_and_parse():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"title": "PIB release", "url": "https://pib.gov.in/r1",
             "content": "snippet one", "score": 0.91},
            {"title": "no url skipped", "content": "x"},
            {"url": "https://example.com/2"},
        ]})

    client = mock_tavily(handler)
    hits = await client.search("rbi repo rate", max_results=4)
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["auth"] == "Bearer tvly-test-key"
    assert seen["body"] == {"query": "rbi repo rate", "max_results": 4}
    assert [h.url for h in hits] == ["https://pib.gov.in/r1",
                                     "https://example.com/2"]
    assert hits[0].title == "PIB release"
    assert hits[0].snippet == "snippet one"
    assert hits[1].title is None
    await client.aclose()


async def test_tavily_max_results_clamped_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["max_results"] == 20:  # the documented cap
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"results": []})

    client = mock_tavily(handler)
    with pytest.raises(SearchError):
        await client.search("q", max_results=99)  # clamped to 20 -> 500
    assert await client.search("q", max_results=0) == []  # floored to 1
    await client.aclose()


# --- _ddg_unwrap -------------------------------------------------------------

def test_unwrap_decodes_uddg_redirect():
    href = ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1"
            "&rut=abc")
    assert _ddg_unwrap(href) == "https://example.com/a?x=1"


def test_unwrap_passes_direct_http():
    assert _ddg_unwrap("https://example.com/p") == "https://example.com/p"


@pytest.mark.parametrize("href", [
    "",
    "javascript:void(0)",
    "//duckduckgo.com/y.js?ad_provider=foo",          # ad, no uddg
    "//duckduckgo.com/l/?rut=abc",                     # /l/ but no uddg
    "/settings",                                       # internal relative
])
def test_unwrap_drops_non_targets(href):
    assert _ddg_unwrap(href) is None


# --- parse_ddg_html ----------------------------------------------------------

def test_parse_extracts_results():
    hits = parse_ddg_html(FIXTURE.read_bytes(), max_results=10)
    urls = [h.url for h in hits]
    assert RBI in urls
    assert LIVEMINT in urls
    assert ET in urls           # direct http href, no uddg wrapper


def test_parse_deduplicates_targets():
    hits = parse_ddg_html(FIXTURE.read_bytes(), max_results=10)
    assert [h.url for h in hits].count(RBI) == 1


def test_parse_drops_ads_and_internal_links():
    hits = parse_ddg_html(FIXTURE.read_bytes(), max_results=10)
    assert all("duckduckgo.com" not in h.url for h in hits)
    assert all("y.js" not in h.url for h in hits)


def test_parse_extracts_title_and_snippet():
    by_url = {h.url: h for h in parse_ddg_html(FIXTURE.read_bytes())}
    assert by_url[RBI].title == "RBI keeps repo rate unchanged at 6.5%"
    assert "easing food inflation" in (by_url[RBI].snippet or "")


def test_parse_respects_max_results():
    assert len(parse_ddg_html(FIXTURE.read_bytes(), max_results=2)) == 2


def test_parse_garbage_yields_empty():
    assert parse_ddg_html(b"<<< not html >>>", max_results=5) == []


# --- create_search_client (factory selection) --------------------------------

def test_factory_prefers_tavily_when_key_present():
    client = create_search_client("tvly-xxx", web_search_enabled=True)
    assert isinstance(client, TavilyClient)


async def test_factory_keyless_default_is_duckduckgo():
    client = create_search_client(None, web_search_enabled=True)
    assert isinstance(client, DuckDuckGoSearchClient)
    assert client.name == "duckduckgo"
    await client.aclose()


async def test_factory_disabled_is_null():
    assert isinstance(
        create_search_client(None, web_search_enabled=False),
        NullSearchClient)
    assert isinstance(
        create_search_client("", web_search_enabled=False),
        NullSearchClient)
    assert await NullSearchClient().search("anything") == []


# --- DuckDuckGoSearchClient over a mocked transport (no real network) --------

async def test_ddg_client_search_parses_mocked_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "html.duckduckgo.com"
        assert b"q=" in request.content
        return httpx.Response(200, content=FIXTURE.read_bytes())

    client = DuckDuckGoSearchClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        hits = await client.search("rbi repo rate", max_results=5)
    finally:
        await client.aclose()
    assert RBI in [h.url for h in hits]


async def test_ddg_client_http_error_raises_search_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    client = DuckDuckGoSearchClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SearchError):
            await client.search("anything")
    finally:
        await client.aclose()
