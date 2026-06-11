"""Web search seam for the analysis engine — SearchClient ABC + Tavily.

The verify stage consumes the ABC and never knows which backend answered.
Keyless deployments get NullSearchClient: verification degrades to
corpus-only and the stage summary records why.

TavilyClient request shape verified against docs.tavily.com (2026-06):
POST https://api.tavily.com/search, ``Authorization: Bearer <key>``, JSON
body {query, max_results}; response {results: [{title, url, content,
score}]}. (The legacy body-field ``api_key`` auth still works; the Bearer
header is the documented current form.)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESULTS_CAP = 20  # Tavily's documented max


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


def create_search_client(api_key: str | None) -> SearchClient:
    """TavilyClient when a key exists, NullSearchClient otherwise."""
    return TavilyClient(api_key) if api_key else NullSearchClient()
