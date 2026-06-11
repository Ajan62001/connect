"""SearchClient seam: Tavily request/response shape against a mocked
transport (no network), the keyless Null degradation, and the factory."""

from __future__ import annotations

import json

import httpx
import pytest

from connect.retrieval.search_client import (
    NullSearchClient,
    SearchError,
    TavilyClient,
    create_search_client,
)


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


async def test_null_client_and_factory():
    assert await NullSearchClient().search("anything") == []
    assert isinstance(create_search_client(None), NullSearchClient)
    assert isinstance(create_search_client(""), NullSearchClient)
    tavily = create_search_client("tvly-key")
    assert isinstance(tavily, TavilyClient)
    await tavily.aclose()
