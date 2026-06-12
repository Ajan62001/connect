"""Web search seam for the analysis + investigation engines — SearchClient
ABC + three backends.

The verify/investigation tools consume the ABC and never know which backend
answered. Backend selection (``create_search_client``):

- ``TavilyClient`` when a TAVILY_API_KEY is set — best quality, paid key.
- ``DuckDuckGoSearchClient`` otherwise (keyless, no signup) — scrapes the
  html.duckduckgo.com results page. The default so web search WORKS out of
  the box; degrades to empty on any block/parse failure rather than erroring
  the whole investigation.
- ``NullSearchClient`` only when web search is explicitly disabled
  (``web_search_enabled=False`` — the test default, keeping suites offline).

TavilyClient request shape verified against docs.tavily.com (2026-06):
POST https://api.tavily.com/search, ``Authorization: Bearer <key>``, JSON
body {query, max_results}; response {results: [{title, url, content,
score}]}. (The legacy body-field ``api_key`` auth still works; the Bearer
header is the documented current form.)

DuckDuckGo ``html`` endpoint (no API key): POST
https://html.duckduckgo.com/html/ with form ``q=<query>``; results are
``<a class="result__a">`` anchors whose href wraps the real target in a
``/l/?uddg=<urlencoded>`` redirect, with a sibling
``<a class="result__snippet">``. Parsing is a pure function over bytes
(``parse_ddg_html``) so it unit-tests against a saved fixture, zero network.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESULTS_CAP = 20  # Tavily's documented max
# DDG returns an empty page to obvious bots; send a normal browser UA.
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class SearchError(Exception):
    """Search backend failure — callers degrade to corpus-only evidence."""


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    title: str | None = None
    snippet: str | None = None


class SearchClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def search(self, query: str, max_results: int = 5,
                     ) -> list[SearchHit]:
        """Top hits for one query; raises SearchError on backend failure."""

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        return None


class NullSearchClient(SearchClient):
    """No API key configured — web search is unavailable, never an error."""

    name = "null"

    async def search(self, query: str, max_results: int = 5,
                     ) -> list[SearchHit]:
        return []


class TavilyClient(SearchClient):
    name = "tavily"

    def __init__(self, api_key: str, *,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}"})

    async def search(self, query: str, max_results: int = 5,
                     ) -> list[SearchHit]:
        body = {"query": query,
                "max_results": max(1, min(max_results, MAX_RESULTS_CAP))}
        try:
            resp = await self._client.post(TAVILY_ENDPOINT, json=body)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as e:
            raise SearchError(f"tavily search failed: {e}") from e
        except ValueError as e:
            raise SearchError(f"tavily returned non-JSON: {e}") from e
        hits: list[SearchHit] = []
        for item in payload.get("results", []) or []:
            url = item.get("url")
            if not url:
                continue
            hits.append(SearchHit(url=url, title=item.get("title"),
                                  snippet=item.get("content")))
        return hits

    async def aclose(self) -> None:
        await self._client.aclose()


def _ddg_unwrap(href: str) -> str | None:
    """Resolve a DuckDuckGo result href to its real target URL.

    DDG wraps targets as ``//duckduckgo.com/l/?uddg=<urlencoded>&rut=...``;
    occasionally a direct ``http(s)`` href appears. Anything else (internal
    DDG links, ads, ``javascript:``) yields None and is dropped.
    """
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    if parsed.netloc.lower().endswith("duckduckgo.com"):
        # Only the /l/?uddg= redirect is a real result; everything else on
        # the duckduckgo.com host (y.js ad slots, internal links, an /l/
        # without a uddg target) is dropped.
        if parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [None])[0]
            if target:
                target = unquote(target)
                return (target if target.startswith(("http://", "https://"))
                        else None)
        return None
    return href if href.startswith(("http://", "https://")) else None


def parse_ddg_html(data: bytes | str, *,
                   max_results: int = 5) -> list[SearchHit]:
    """Pure: DuckDuckGo html-results bytes -> SearchHits (deduped by URL).

    Tolerant of markup drift and garbage — a page it cannot parse yields []
    (web search degrades to corpus-only, never raises)."""
    from lxml import html as lxml_html  # lazy heavy import

    try:
        root = lxml_html.fromstring(data)
    except Exception:
        return []
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for result in root.xpath('//div[contains(@class, "result")]'):
        anchors = result.xpath('.//a[contains(@class, "result__a")]')
        if not anchors:
            continue
        url = _ddg_unwrap(anchors[0].get("href") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = " ".join(anchors[0].text_content().split()) or None
        snip = result.xpath('.//a[contains(@class, "result__snippet")]')
        snippet = (" ".join(snip[0].text_content().split())
                   if snip else None) or None
        hits.append(SearchHit(url=url, title=title, snippet=snippet))
        if len(hits) >= max_results:
            break
    return hits


class DuckDuckGoSearchClient(SearchClient):
    """Keyless web search via the DuckDuckGo html endpoint. No API key, no
    signup — the default backend so investigations can reach the open web
    out of the box. Rate-limited and best-effort: a block returns SearchError
    (the tool degrades to corpus-only for that call), an unparseable page
    returns []."""

    name = "duckduckgo"

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA,
                     "Accept": "text/html,application/xhtml+xml",
                     "Referer": "https://duckduckgo.com/"})

    async def search(self, query: str, max_results: int = 5,
                     ) -> list[SearchHit]:
        n = max(1, min(max_results, MAX_RESULTS_CAP))
        try:
            resp = await self._client.post(DDG_HTML_ENDPOINT,
                                           data={"q": query})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise SearchError(f"duckduckgo search failed: {e}") from e
        return parse_ddg_html(resp.content, max_results=n)

    async def aclose(self) -> None:
        await self._client.aclose()


def create_search_client(api_key: str | None, *,
                         web_search_enabled: bool = True) -> SearchClient:
    """TavilyClient when a key exists; keyless DuckDuckGoSearchClient when
    web search is enabled (the default); NullSearchClient only when web
    search is explicitly turned off (tests, air-gapped deployments)."""
    if api_key:
        return TavilyClient(api_key)
    if web_search_enabled:
        return DuckDuckGoSearchClient()
    return NullSearchClient()
