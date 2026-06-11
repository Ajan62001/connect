"""Telegram public-channel adapter — t.me/s/<channel> preview pages.

Free, no key: Telegram serves a public HTML preview of channels at
https://t.me/s/<channel> (message blocks in ``tgme_widget_message`` markup:
``data-post="<channel>/<msg_id>"``, a ``tgme_widget_message_text`` div, and
a ``<time datetime="...">`` stamp). Fetched through the shared polite
Fetcher with ignore_robots=True — a user-registered channel is a
feed-reader request, not autonomous crawling (the rss adapter's rationale).

``parse_channel_page`` is pure (bytes -> items) so tests run over an HTML
fixture with zero network. Each message block's own HTML is the document's
raw blob.

Cursor (stated choice): the poller passes source.last_polled_at and posts
whose <time datetime> is older are filtered out (``uses_since``). The
preview page only carries the ~20 latest posts and every post URL
(https://t.me/<channel>/<msg_id>) is URL-deduped at ingest, so the time
filter is a cheap skip — idempotency never depends on it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from connect.domain.models import DiscoveredItem
from connect.ingestion.fetcher import Fetcher
from connect.sources.base import SourceConfig, TelegramConfig, parse_source_config

_TITLE_CHARS = 80


def _normalize_dt(value: str | None) -> str | None:
    """t.me datetime attr ('2026-06-10T18:05:00+00:00') -> ISO-8601 UTC Z."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_channel_page(data: bytes | str, channel: str) -> list[DiscoveredItem]:
    """Pure: t.me/s/<channel> HTML -> items, newest first (the page lists
    oldest->newest). Media-only posts (no text block) are skipped — there
    is nothing to index."""
    from lxml import html as lxml_html  # lazy heavy import

    try:
        root = lxml_html.fromstring(data)
    except Exception:  # noqa: BLE001 — unparseable page yields no items
        return []

    items: list[DiscoveredItem] = []
    for block in root.xpath(
            "//div[contains(@class, 'tgme_widget_message') and @data-post]"):
        post = block.get("data-post") or ""        # "<channel>/<msg_id>"
        msg_id = post.rsplit("/", 1)[-1]
        if not msg_id.isdigit():
            continue
        text_divs = block.xpath(
            ".//div[contains(@class, 'tgme_widget_message_text')]")
        if not text_divs:
            continue
        text_div = text_divs[0]
        for br in text_div.xpath(".//br"):  # keep line breaks readable
            br.tail = "\n" + (br.tail or "")
        text = text_div.text_content().strip()
        if not text:
            continue
        links: list[str] = []
        for a in text_div.xpath(".//a[@href]"):
            href = (a.get("href") or "").strip()
            if href.startswith(("http://", "https://")) and href not in links:
                links.append(href)
        times = block.xpath(".//time[@datetime]")
        published = _normalize_dt(times[0].get("datetime")) if times else None
        headline = " ".join(text.split())[:_TITLE_CHARS]
        items.append(DiscoveredItem(
            title=f"{channel}: {headline}",
            url=f"https://t.me/{channel}/{msg_id}",
            published_at=published,
            content_text=text,
            author=channel,
            media_type="telegram",
            raw=lxml_html.tostring(block),
            link_urls=tuple(links),
        ))
    items.reverse()
    return items


class TelegramAdapter:
    type_name = "telegram"
    uses_since = True  # last_polled_at time filter (module docstring)

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def validate(self, config: dict[str, Any]) -> SourceConfig:
        return parse_source_config("telegram", config)

    async def discover(self, config: dict[str, Any],
                       since: str | None = None) -> list[DiscoveredItem]:
        cfg = self.validate(config)
        assert isinstance(cfg, TelegramConfig)
        result = await self.fetcher.fetch(
            f"https://t.me/s/{cfg.channel}", ignore_robots=True)
        items = parse_channel_page(result.content, cfg.channel)
        if since:
            items = [i for i in items
                     if i.published_at is None or i.published_at > since]
        return items

    async def sample(self, config: dict[str, Any],
                     limit: int = 5) -> list[DiscoveredItem]:
        return (await self.discover(config))[:limit]
