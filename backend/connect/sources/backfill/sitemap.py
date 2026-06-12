"""Sitemap backfill: enumerate historical article URLs from a site's
sitemap(s).

``parse_sitemap`` is a pure function (XML bytes -> entries) so it unit-tests
against fixtures with zero network. ``discover_sitemap_urls`` walks a sitemap
index recursively (bounded by ``max_sitemaps``), date-filtering child
sitemaps by their ``<lastmod>`` and individual URLs by ``<lastmod>`` /
``<news:publication_date>``.

Robots: sitemaps are fetched with ``ignore_robots=True`` — an admin
explicitly triggered the backfill and sitemaps exist precisely to be
machine-read. Discovered ARTICLE urls are later ingested through the normal
Fetcher path, which DOES enforce robots.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from connect.sources.backfill.candidates import (
    BackfillCandidate,
    in_window,
    norm_date,
)

log = logging.getLogger(__name__)

# Defensive caps: a hostile/huge sitemap tree cannot fan out unbounded.
DEFAULT_MAX_SITEMAPS = 25
DEFAULT_LIMIT = 1000


_ROBOTS_SITEMAP_RE = re.compile(rb"(?im)^\s*sitemap:\s*(\S+)\s*$")


def extract_robots_sitemaps(data: bytes | str) -> list[str]:
    """Pull ``Sitemap: <url>`` directives out of a robots.txt body (deduped,
    order-preserved) — the canonical place sites advertise their sitemaps."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    out: list[str] = []
    seen: set[str] = set()
    for m in _ROBOTS_SITEMAP_RE.finditer(data):
        url = m.group(1).decode("utf-8", "replace").strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def parse_sitemap(data: bytes | str) -> tuple[str, list[tuple[str, str | None]]]:
    """Parse a sitemap XML.

    Returns ``(kind, entries)`` where kind is ``"index"`` (entries are child
    sitemap locs), ``"urlset"`` (entries are page locs), or ``""`` (not a
    sitemap / unparseable). Each entry is ``(loc, date_or_None)`` — the date
    is the news publication_date when present, else lastmod, normalized to
    ``YYYY-MM-DD``. Namespace-agnostic (matches by local element name)."""
    from lxml import etree

    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        root = etree.fromstring(
            data, parser=etree.XMLParser(
                recover=True, resolve_entities=False, no_network=True))
    except (etree.XMLSyntaxError, ValueError):
        return ("", [])
    if root is None or not isinstance(root.tag, str):
        return ("", [])

    rootname = etree.QName(root).localname
    entries: list[tuple[str, str | None]] = []

    if rootname == "sitemapindex":
        for node in root:
            if not isinstance(node.tag, str):
                continue
            loc_el = node.find("{*}loc")
            if loc_el is None or not (loc_el.text or "").strip():
                continue
            mod_el = node.find("{*}lastmod")
            entries.append((loc_el.text.strip(),
                            norm_date(mod_el.text if mod_el is not None else None)))
        return ("index", entries)

    if rootname == "urlset":
        for node in root:
            if not isinstance(node.tag, str):
                continue
            loc_el = node.find("{*}loc")
            if loc_el is None or not (loc_el.text or "").strip():
                continue
            # news sitemaps carry a precise <news:publication_date>; prefer it
            pub_el = node.find(".//{*}publication_date")
            mod_el = node.find("{*}lastmod")
            d = (norm_date(pub_el.text if pub_el is not None else None)
                 or norm_date(mod_el.text if mod_el is not None else None))
            entries.append((loc_el.text.strip(), d))
        return ("urlset", entries)

    return ("", [])


async def discover_sitemap_urls(
        fetcher: Any, *,
        root_urls: Sequence[str],
        start: str | None = None,
        end: str | None = None,
        limit: int = DEFAULT_LIMIT,
        link_pattern: str | None = None,
        max_sitemaps: int = DEFAULT_MAX_SITEMAPS) -> list[BackfillCandidate]:
    """Walk sitemap(s) from ``root_urls`` (a sitemap index or urlset),
    returning article candidates inside the date window that match
    ``link_pattern`` (when given). Child sitemaps whose ``<lastmod>`` is
    known and clearly older than ``start`` are pruned (monthly-partitioned
    news sitemaps); unknown-lastmod children are always followed."""
    pat = re.compile(link_pattern) if link_pattern else None
    queue: list[str] = list(root_urls)
    seen_sitemaps: set[str] = set()
    seen_urls: set[str] = set()
    candidates: list[BackfillCandidate] = []
    fetched = 0

    while queue and fetched < max_sitemaps and len(candidates) < limit:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        try:
            res = await fetcher.fetch(sm_url, ignore_robots=True)
        except Exception as e:  # noqa: BLE001 — a missing/blocked sitemap is data
            log.debug("backfill sitemap fetch failed %s: %s", sm_url, e)
            continue
        fetched += 1
        kind, entries = parse_sitemap(res.content)

        if kind == "index":
            for loc, mod in entries:
                # prune only when the child is KNOWN to predate the window
                if start and mod is not None and mod < (norm_date(start) or ""):
                    continue
                if loc not in seen_sitemaps:
                    queue.append(loc)
        elif kind == "urlset":
            for loc, d in entries:
                if loc in seen_urls:
                    continue
                if pat and not pat.search(loc):
                    continue
                if in_window(d, start, end) is False:
                    continue
                seen_urls.add(loc)
                candidates.append(BackfillCandidate(
                    url=loc, published_at=d, via="sitemap"))
                if len(candidates) >= limit:
                    break

    return candidates
