"""RSS adapter — feedparser over fetcher-fetched bytes.

``parse_feed`` is a pure function (bytes -> items) so tests run it over a
fixture file with zero network.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from connect.domain.models import DiscoveredItem
from connect.ingestion.fetcher import Fetcher
from connect.sources.base import RssConfig, SourceConfig, parse_source_config


def _entry_timestamp(entry: Any) -> str | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None) or (
            entry.get(attr) if isinstance(entry, dict) else None)
        if parsed:
            try:
                return datetime.fromtimestamp(
                    time.mktime(parsed), tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, OverflowError):
                continue
    return None


def parse_feed(data: bytes) -> list[DiscoveredItem]:
    """Pure: feed bytes -> DiscoveredItems (skips entries without a link)."""
    import feedparser  # lazy import

    feed = feedparser.parse(data)
    items: list[DiscoveredItem] = []
    for entry in feed.entries:
        url = entry.get("link")
        if not url:
            continue
        items.append(DiscoveredItem(
            title=(entry.get("title") or url).strip(),
            url=url,
            published_at=_entry_timestamp(entry),
            summary=(entry.get("summary") or None),
        ))
    return items


class RssAdapter:
    type_name = "rss"

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def validate(self, config: dict[str, Any]) -> SourceConfig:
        return parse_source_config("rss", config)

    async def discover(self, config: dict[str, Any],
                       since: str | None = None) -> list[DiscoveredItem]:
        cfg = self.validate(config)
        assert isinstance(cfg, RssConfig)
        # The feed URL is a user-registered endpoint — fetching it is a
        # feed-reader request, not crawling (news.google.com robots-disallows
        # everything yet publishes RSS for exactly this use). Item links
        # discovered in the feed keep full robots enforcement.
        result = await self.fetcher.fetch(cfg.feed_url, ignore_robots=True)
        items = parse_feed(result.content)
        if since:
            items = [i for i in items
                     if i.published_at is None or i.published_at > since]
        return items

    async def sample(self, config: dict[str, Any],
                     limit: int = 5) -> list[DiscoveredItem]:
        return (await self.discover(config))[:limit]
