"""Historical backfill — get OLDER documents into the corpus than the live
feed/index window exposes.

Four discovery strategies, composable per run (design: the user asked for
"all of them" to enrich the corpus for analysis + fact-check):

- ``sitemap``    — walk the site's sitemap(s) by date  (sitemap.py)
- ``pagination`` — page a news listing backward         (pagination.py)
- ``wayback``    — enumerate archived URLs via CDX       (wayback.py)
- ``manual``     — explicit URLs / date-templated URLs   (manual.py)

Every strategy emits ``BackfillCandidate``s; ``run_backfill`` merges + dedups
them, applies the date window, and ingests each through the ONE canonical
corpus-write path (``pipeline.ingest_url`` with ``origin="backfill"``) —
reusing existing dedup (idempotent/resumable), robots-respecting fetch, and
T0 hooks. Candidates are attached to the real source (tier 1-3), optionally
re-resolved per-URL to a registered domain via ``source_resolver`` so an
article fetched here gets proper credibility provenance.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from connect.sources.backfill.candidates import (
    BackfillCandidate,
    dedup_candidates,
    passes_window,
)
from connect.sources.backfill.manual import expand_manual_urls
from connect.sources.backfill.pagination import discover_paginated_urls
from connect.sources.backfill.sitemap import (
    discover_sitemap_urls,
    extract_robots_sitemaps,
)
from connect.sources.backfill.wayback import discover_wayback_urls
from connect.storage import documents as doc_dao

log = logging.getLogger(__name__)

METHODS = ("sitemap", "pagination", "wayback", "manual")
DEFAULT_LIMIT = 200
# check the cancel flag every N ingests during a long backfill
_CANCEL_EVERY = 10

SourceResolver = Callable[[Any, str], Awaitable[int | None]]


def _config_url(source: Any) -> str | None:
    cfg = source.config or {}
    return cfg.get("index_url") or cfg.get("feed_url")


def _source_base_url(source: Any) -> str | None:
    """``scheme://host`` of the source's configured URL (for sitemap/wayback
    host enumeration). None when the source carries no URL."""
    u = _config_url(source)
    if not u:
        return None
    p = urlsplit(u)
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def default_sitemap_roots(base_url: str) -> list[str]:
    """Common sitemap entry points to probe (the robots.txt Sitemap: lines
    are merged in by run_backfill when reachable)."""
    return [
        f"{base_url}/sitemap.xml",
        f"{base_url}/sitemap_index.xml",
        f"{base_url}/news-sitemap.xml",
        f"{base_url}/sitemap-news.xml",
    ]


async def _discover(fetcher: Any, *, source: Any, methods: list[str],
                    start: str | None, end: str | None, limit: int,
                    max_pages: int, page_template: str | None,
                    sitemap_urls: list[str], manual_urls: list[str],
                    manual_url_template: str | None,
                    ) -> list[BackfillCandidate]:
    cfg = source.config or {}
    base = _source_base_url(source)
    link_pattern = cfg.get("link_pattern")
    index_url = cfg.get("index_url")
    found: list[BackfillCandidate] = []

    if "sitemap" in methods:
        roots = list(sitemap_urls)
        if not roots and base:
            roots = default_sitemap_roots(base)
            # robots.txt often advertises the real sitemap locations
            try:
                rob = await fetcher.fetch(f"{base}/robots.txt",
                                          ignore_robots=True)
                roots = extract_robots_sitemaps(rob.content) + roots
            except Exception:  # noqa: BLE001 — robots optional
                pass
        if roots:
            found += await discover_sitemap_urls(
                fetcher, root_urls=roots, start=start, end=end, limit=limit,
                link_pattern=link_pattern)

    if "pagination" in methods and index_url and link_pattern:
        found += await discover_paginated_urls(
            fetcher, index_url=index_url, link_pattern=link_pattern,
            page_template=page_template, max_pages=max_pages,
            start=start, end=end)

    if "wayback" in methods and (base or index_url):
        found += await discover_wayback_urls(
            fetcher, target_url=base or index_url, start=start, end=end,
            limit=limit)

    if "manual" in methods and (manual_urls or manual_url_template):
        found += expand_manual_urls(
            urls=manual_urls, url_template=manual_url_template,
            start=start, end=end)

    return found


async def run_backfill(
        conn: Any, *, source: Any, pipeline: Any, fetcher: Any,
        methods: list[str] | None = None,
        start: str | None = None, end: str | None = None,
        limit: int = DEFAULT_LIMIT, max_pages: int = 10,
        page_template: str | None = None,
        sitemap_urls: list[str] | None = None,
        manual_urls: list[str] | None = None,
        manual_url_template: str | None = None,
        source_resolver: SourceResolver | None = None,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        ) -> dict[str, Any]:
    """Discover historical candidates via the requested ``methods`` and
    ingest them. Returns a result dict (also rendered to a status string by
    the worker handler). Idempotent: already-known URLs are skipped via
    ``url_exists`` and the pipeline's content-hash dedup."""
    methods = [m for m in (methods or list(METHODS)) if m in METHODS]
    candidates = await _discover(
        fetcher, source=source, methods=methods, start=start, end=end,
        limit=max(1, limit), max_pages=max_pages, page_template=page_template,
        sitemap_urls=sitemap_urls or [], manual_urls=manual_urls or [],
        manual_url_template=manual_url_template)

    # window-filter (unknown dates pass), dedup by URL, cap to limit
    windowed = [c for c in candidates if passes_window(c.published_at, start, end)]
    deduped = dedup_candidates(windowed)[:max(1, limit)]

    new = dups = errors = 0
    cancelled = False
    for idx, cand in enumerate(deduped):
        if should_cancel and idx % _CANCEL_EVERY == 0 and await should_cancel():
            cancelled = True
            break
        if await doc_dao.url_exists(conn, cand.url):
            dups += 1
            continue
        source_id = source.id
        if source_resolver is not None:
            resolved = await source_resolver(conn, cand.url)
            if resolved is not None:
                source_id = resolved
        try:
            title = cand.title if cand.title and cand.title != cand.url else None
            result = await pipeline.ingest_url(
                conn, cand.url, source_id=source_id, title=title,
                published_at_hint=cand.published_at, origin="backfill")
            if result.created:
                new += 1
            else:
                dups += 1
        except Exception as e:  # noqa: BLE001 — per-URL failure never aborts the run
            errors += 1
            log.warning("backfill ingest failed %s: %s", cand.url, e)

    return {
        "discovered": len(deduped),
        "new": new, "dups": dups, "errors": errors,
        "methods": methods, "cancelled": cancelled,
    }
