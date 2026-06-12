"""Wayback Machine backfill: enumerate historical URLs for a domain from the
Internet Archive CDX API, bounded by a date window.

The CDX API (``http://web.archive.org/cdx/search/cdx``) returns a JSON table
— a header row then ``[original, timestamp, statuscode]`` rows. We keep the
ORIGINAL live URLs (not snapshot URLs) so ingestion attaches them to the
real source/domain with clean canonical URLs; dead originals simply fail the
later live fetch and are counted as errors. ``parse_cdx_json`` is pure for
fixture testing.

Good for sites without a usable sitemap, or to recover articles that have
rotated off the live index entirely.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode, urlparse

from connect.sources.backfill.candidates import BackfillCandidate, norm_date

log = logging.getLogger(__name__)

CDX_ENDPOINT = "http://web.archive.org/cdx/search/cdx"
DEFAULT_LIMIT = 1000


def _compact(iso: str | None) -> str | None:
    """'YYYY-MM-DD' -> 'YYYYMMDD' (the CDX from/to format)."""
    d = norm_date(iso)
    return d.replace("-", "") if d else None


def build_cdx_url(target_url: str, *, start: str | None = None,
                  end: str | None = None, limit: int = DEFAULT_LIMIT) -> str:
    """Construct the CDX query for every archived html URL on the target's
    host, status 200, within the date window, collapsed to one row per URL."""
    host = urlparse(target_url).netloc or target_url.strip("/")
    params: list[tuple[str, str]] = [
        ("url", f"{host}/*"),
        ("output", "json"),
        ("fl", "original,timestamp,statuscode"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"),
        ("collapse", "urlkey"),
        ("limit", str(max(1, limit))),
    ]
    if (cf := _compact(start)):
        params.append(("from", cf))
    if (ct := _compact(end)):
        params.append(("to", ct))
    return f"{CDX_ENDPOINT}?{urlencode(params)}"


def parse_cdx_json(data: bytes | str) -> list[BackfillCandidate]:
    """CDX JSON bytes -> candidates (deduped by URL). The first row is a
    header naming the columns; we locate 'original' and 'timestamp' by name
    so column-order changes don't break us. Garbage -> []."""
    try:
        rows = json.loads(data)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    header = rows[0]
    if not isinstance(header, list):
        return []
    try:
        oi = header.index("original")
    except ValueError:
        return []
    ti = header.index("timestamp") if "timestamp" in header else None

    out: list[BackfillCandidate] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if not isinstance(row, list) or len(row) <= oi:
            continue
        url = (row[oi] or "").strip()
        if not url or url in seen:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        ts = row[ti] if ti is not None and len(row) > ti else None
        out.append(BackfillCandidate(
            url=url, published_at=norm_date(ts), via="wayback"))
    return out


async def discover_wayback_urls(
        fetcher: Any, *,
        target_url: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = DEFAULT_LIMIT) -> list[BackfillCandidate]:
    """Query the CDX API via the polite Fetcher and return candidates. A
    failed/empty CDX response yields [] (never raises into the backfill)."""
    cdx_url = build_cdx_url(target_url, start=start, end=end, limit=limit)
    try:
        res = await fetcher.fetch(cdx_url, ignore_robots=True)
    except Exception as e:  # noqa: BLE001 — archive outage is data, not fatal
        log.debug("backfill wayback CDX fetch failed %s: %s", cdx_url, e)
        return []
    return parse_cdx_json(res.content)[:limit]
