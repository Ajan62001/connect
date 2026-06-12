"""Pagination backfill: walk a news listing page backward through its pages
(``?page=2``, ``?page=3`` …) reusing the web_news ``parse_news_index``
extractor. Stops at ``max_pages``, when a page yields no NEW article links
(pagination exhausted / loops back to page 1), or when extracted URL dates
fall before the window.

web_news listings rarely carry per-item timestamps, so date filtering is
best-effort: when an article URL embeds a /YYYY/MM/DD/ path the candidate is
window-filtered on it; otherwise it passes (URL-dedup keeps ingest
idempotent).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from connect.sources.adapters.web_news import parse_news_index
from connect.sources.backfill.candidates import (
    BackfillCandidate,
    date_from_url,
    in_window,
)

log = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 10


def page_url(index_url: str, page: int, *, template: str | None = None) -> str:
    """The URL for page N. With a ``template`` containing ``{page}`` it is
    formatted directly; otherwise a ``page=N`` query param is appended (page
    1 is the bare index_url). The template wins because real sites vary
    (``/news/page/2/``, ``?pg=2``, ``?from=20`` …)."""
    if template and "{page}" in template:
        return template.replace("{page}", str(page))
    if page <= 1:
        return index_url
    parts = urlsplit(index_url)
    query = f"{parts.query}&page={page}" if parts.query else f"page={page}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query,
                       parts.fragment))


async def discover_paginated_urls(
        fetcher: Any, *,
        index_url: str,
        link_pattern: str,
        page_template: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        start: str | None = None,
        end: str | None = None,
        start_page: int = 1) -> list[BackfillCandidate]:
    """Page through the listing, collecting article candidates. A page that
    fetch-fails or yields zero NEW links ends the walk."""
    seen: set[str] = set()
    candidates: list[BackfillCandidate] = []

    for page in range(start_page, start_page + max_pages):
        url = page_url(index_url, page, template=page_template)
        try:
            res = await fetcher.fetch(url, ignore_robots=True)
        except Exception as e:  # noqa: BLE001 — a 404 past the last page ends it
            log.debug("backfill pagination fetch stopped at %s: %s", url, e)
            break
        items = parse_news_index(res.content, link_pattern=link_pattern,
                                 base_url=index_url)
        fresh = [i for i in items if i.url and i.url not in seen]
        if not fresh:
            break  # no new links -> exhausted (or paged back to the start)
        for i in fresh:
            seen.add(i.url)
            d = date_from_url(i.url)
            if in_window(d, start, end) is False:
                continue
            candidates.append(BackfillCandidate(
                url=i.url, title=i.title, published_at=d, via="pagination"))

    return candidates
