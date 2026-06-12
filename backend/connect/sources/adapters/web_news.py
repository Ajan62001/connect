"""HTML news-listing adapter (web_news source type).

Fetches a news index page, walks every <a href>, filters by a
user-supplied regex, deduplicates, and emits DiscoveredItems whose
``content_text`` is None — signalling the poller to call
``pipeline.ingest_url`` so the full article is fetched and extracted
through the normal ingestion path.

Title extraction order (first non-empty wins):
  1. The link element's own text content.
  2. The text of the nearest ancestor <h1>–<h4>.
  3. The final path segment of the URL (slug, human-readable fallback).

``parse_news_index`` is a pure function (bytes -> items) so tests run it
over a fixture file with zero network.

Robots discipline: the index page is fetched with ``ignore_robots=True``
(same rationale as RSS and Telegram — the admin explicitly registered the
URL, it is a feed-reader request not autonomous crawling). Article URLs
discovered from the page go through the normal Fetcher/link-follow path
which DOES enforce robots.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from connect.domain.models import DiscoveredItem
from connect.ingestion.fetcher import Fetcher
from connect.sources.base import SourceConfig, WebNewsConfig, parse_source_config

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})
_MAX_TITLE = 200


def _slug_title(url: str) -> str:
    """Last non-empty path segment, dashes to spaces, truncated."""
    path = urlparse(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1]
    # strip numeric suffix and .html if present
    segment = re.sub(r"-\d+$", "", segment.replace(".html", ""))
    return segment.replace("-", " ").strip()[:_MAX_TITLE]


def parse_news_index(data: bytes | str, *,
                     link_pattern: str,
                     base_url: str = "") -> list[DiscoveredItem]:
    """Pure: HTML bytes of a news index -> DiscoveredItems (article links).

    Only absolute ``http(s)`` hrefs matching ``link_pattern`` are kept.
    Relative hrefs are resolved against ``base_url`` when provided.
    Duplicate URLs are collapsed (first occurrence wins).
    """
    from lxml import html as lxml_html  # lazy heavy import

    try:
        root = lxml_html.fromstring(data)
    except Exception:  # unparseable page yields no items
        return []

    pat = re.compile(link_pattern)
    seen: set[str] = set()
    items: list[DiscoveredItem] = []

    for a in root.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        # resolve relative URLs if a base is known
        if href.startswith("/") and base_url:
            href = urljoin(base_url, href)

        if not href.startswith(("http://", "https://")):
            continue
        if not pat.search(href):
            continue
        if href in seen:
            continue
        seen.add(href)

        # title: link text -> nearest ancestor heading -> slug
        title = " ".join(a.text_content().split())
        if not title:
            for ancestor in a.iterancestors():
                tag = getattr(ancestor, "tag", None)
                if isinstance(tag, str) and tag.lower() in _HEADING_TAGS:
                    title = " ".join(ancestor.text_content().split())
                    break
        if not title:
            title = _slug_title(href)

        items.append(DiscoveredItem(title=title[:_MAX_TITLE], url=href))

    return items


class WebNewsAdapter:
    type_name = "web_news"

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def validate(self, config: dict[str, Any]) -> SourceConfig:
        return parse_source_config("web_news", config)

    async def discover(self, config: dict[str, Any],
                       since: str | None = None) -> list[DiscoveredItem]:
        cfg = self.validate(config)
        assert isinstance(cfg, WebNewsConfig)
        result = await self.fetcher.fetch(cfg.index_url, ignore_robots=True)
        items = parse_news_index(result.content,
                                 link_pattern=cfg.link_pattern,
                                 base_url=cfg.index_url)
        # web_news items carry no timestamp — URL-dedup at ingest handles
        # idempotency; the since filter is a no-op but kept for protocol
        # symmetry (items with None published_at always pass).
        if since:
            items = [i for i in items
                     if i.published_at is None or i.published_at > since]
        return items

    async def sample(self, config: dict[str, Any],
                     limit: int = 5) -> list[DiscoveredItem]:
        return (await self.discover(config))[:limit]
