"""X/Twitter adapter — twitterapi.io advanced search, ONE batched call.

Endpoint (verified against docs.twitterapi.io, June 2026):

    GET https://api.twitterapi.io/twitter/tweet/advanced_search
        header  X-API-Key: <key>
        params  query, queryType=Latest, cursor ("" = first page)
        200     {"tweets": [...], "has_next_page": bool, "next_cursor": str}
        page    <= 20 tweets (may be fewer after ad filtering)

COST DISCIPLINE (~$2-5/month posture): exactly one search call per source
row per poll cycle — every handle is OR-batched into a single query with a
``since_time`` cursor (source.last_polled_at as epoch; first poll looks
back 24h). Pagination continues only while the page came back FULL and a
next-page cursor exists, hard-capped at MAX_PAGES. Never per-handle
timeline calls.

``parse_search_page`` is pure (payload dict -> items) so tests run it over
a JSON fixture with zero network; the raw tweet JSON rides along on each
item and becomes the document's raw blob (provider-swap insurance).

Missing settings.twitterapi_io_api_key => discover()/sample() raise
AdapterSkip: the poller records 'skipped: TWITTERAPI_IO_API_KEY not set'
and POST /sources/test returns ok=false with that reason. No exceptions
escape, nothing is retried.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from connect.domain.models import DiscoveredItem
from connect.sources.base import (
    AdapterSkip,
    SourceConfig,
    TwitterConfig,
    parse_source_config,
)

API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
PAGE_SIZE = 20            # documented max tweets per page
MAX_PAGES = 5             # hard safety cap per poll cycle
FIRST_POLL_LOOKBACK_HOURS = 24
_TITLE_CHARS = 80


def since_epoch(since: str | None) -> int:
    """ISO last_polled_at -> unix epoch for since_time; first poll (or an
    unparsable cursor) looks back FIRST_POLL_LOOKBACK_HOURS."""
    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass
    lookback = timedelta(hours=FIRST_POLL_LOOKBACK_HOURS)
    return int((datetime.now(timezone.utc) - lookback).timestamp())


def build_query(handles: list[str], epoch: int) -> str:
    """The single batched query. Parenthesized because X search syntax binds
    AND tighter than OR — unparenthesized, since_time would scope to the
    LAST handle only."""
    froms = " OR ".join(f"from:{h}" for h in handles)
    return f"({froms}) since_time:{epoch}"


def _tweet_timestamp(created_at: Any) -> str | None:
    """'Tue Dec 10 07:00:30 +0000 2024' (the documented createdAt format)
    -> ISO-8601 UTC; None when absent/unparsable."""
    if not created_at:
        return None
    try:
        dt = datetime.strptime(str(created_at), "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expanded_urls(tweet: dict) -> tuple[str, ...]:
    """entities.urls[].expanded_url, deduped in order — never t.co."""
    urls: list[str] = []
    for u in (tweet.get("entities") or {}).get("urls") or []:
        expanded = (u.get("expanded_url") or "").strip()
        if expanded and "//t.co/" not in expanded and expanded not in urls:
            urls.append(expanded)
    return tuple(urls)


def tweet_to_item(tweet: dict) -> DiscoveredItem | None:
    """One raw tweet -> a pre-fetched DiscoveredItem (None when the payload
    is missing its identity fields — defensive, never raises)."""
    author = tweet.get("author") or {}
    handle = (author.get("userName") or "").strip()
    tweet_id = str(tweet.get("id") or "").strip()
    text = (tweet.get("text") or "").strip()
    if not handle or not tweet_id or not text:
        return None
    content = text
    quoted = tweet.get("quoted_tweet") or {}
    quoted_text = (quoted.get("text") or "").strip()
    if quoted_text:
        quoted_handle = ((quoted.get("author") or {}).get("userName")
                         or "unknown").strip() or "unknown"
        content += f"\n\n[quoting @{quoted_handle}]: {quoted_text}"
    headline = " ".join(text.split())[:_TITLE_CHARS]
    return DiscoveredItem(
        title=f"@{handle}: {headline}",
        url=f"https://x.com/{handle}/status/{tweet_id}",
        published_at=_tweet_timestamp(tweet.get("createdAt")),
        content_text=content,
        author=f"@{handle}",
        media_type="tweet",
        raw=json.dumps(tweet, ensure_ascii=False).encode("utf-8"),
        link_urls=_expanded_urls(tweet),
    )


def parse_search_page(payload: dict) -> list[DiscoveredItem]:
    """Pure: one advanced-search response page -> items (API order kept —
    Latest returns newest first)."""
    items: list[DiscoveredItem] = []
    for tweet in payload.get("tweets") or []:
        item = tweet_to_item(tweet)
        if item is not None:
            items.append(item)
    return items


class TwitterAdapter:
    type_name = "twitter"
    uses_since = True  # the poller passes source.last_polled_at as the cursor

    def __init__(self, fetcher: Any = None, *, api_key: str | None = None,
                 client: httpx.AsyncClient | None = None):
        self.fetcher = fetcher  # unused (JSON API, not pages); uniform ctor
        self.api_key = api_key
        self._client = client   # injectable for tests (httpx.MockTransport)

    def validate(self, config: dict[str, Any]) -> SourceConfig:
        return parse_source_config("twitter", config)

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def discover(self, config: dict[str, Any],
                       since: str | None = None) -> list[DiscoveredItem]:
        cfg = self.validate(config)
        assert isinstance(cfg, TwitterConfig)
        if not self.api_key:
            raise AdapterSkip("TWITTERAPI_IO_API_KEY not set")
        query = build_query(cfg.handles, since_epoch(since))
        items: list[DiscoveredItem] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            resp = await self._client_or_create().get(
                API_URL,
                params={"query": query, "queryType": "Latest",
                        "cursor": cursor},
                headers={"X-API-Key": self.api_key})
            resp.raise_for_status()
            payload = resp.json()
            page_tweets = payload.get("tweets") or []
            items.extend(parse_search_page(payload))
            cursor = payload.get("next_cursor") or ""
            # paginate only on a FULL page with a next-page token
            if not (payload.get("has_next_page") and cursor
                    and len(page_tweets) == PAGE_SIZE):
                break
        return items

    async def sample(self, config: dict[str, Any],
                     limit: int = 5) -> list[DiscoveredItem]:
        return (await self.discover(config))[:limit]
