"""Shared types + date helpers for historical backfill.

A ``BackfillCandidate`` is one discovered historical article URL (plus an
optional publish date + title) that the orchestrator funnels through the
normal ``pipeline.ingest_url`` path. All four discovery strategies
(sitemap / pagination / wayback / manual) emit candidates; the orchestrator
dedups them by URL, applies the date window, and ingests.

Dates are normalized to ``YYYY-MM-DD`` strings (the corpus' published_at
shape). ``in_window`` is three-valued: True / False / None(unknown) — an
unknown date never excludes a candidate (URL-dedup keeps ingestion
idempotent regardless)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# Max days a single date-range expansion will enumerate (manual templating /
# safety bound — a runaway range cannot enqueue an unbounded crawl).
MAX_RANGE_DAYS = 1000


@dataclass(frozen=True)
class BackfillCandidate:
    url: str
    published_at: str | None = None   # ISO YYYY-MM-DD
    title: str | None = None
    via: str = ""                     # discovery strategy (provenance/logging)


_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")          # wayback 14-digit ts
_URL_DATE_RE = re.compile(r"/(\d{4})[/-](\d{2})[/-](\d{2})(?:[/-]|$)")


def _valid_ymd(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def norm_date(value: str | None) -> str | None:
    """Best-effort -> 'YYYY-MM-DD'. Accepts ISO datetimes, plain ISO dates,
    and compact 14-digit Wayback timestamps. Unparseable -> None."""
    if not value:
        return None
    s = str(value).strip()
    m = _ISO_RE.match(s) or _ISO_RE.search(s)
    if m:
        return _valid_ymd(int(m[1]), int(m[2]), int(m[3]))
    m = _COMPACT_RE.match(s)
    if m:
        return _valid_ymd(int(m[1]), int(m[2]), int(m[3]))
    return None


def date_from_url(url: str) -> str | None:
    """Pull a /YYYY/MM/DD/ (or YYYY-MM-DD) date embedded in a URL path —
    many news permalinks carry one, giving pagination/manual candidates a
    real publish date for window filtering and timeline features."""
    m = _URL_DATE_RE.search(url)
    if not m:
        return None
    return _valid_ymd(int(m[1]), int(m[2]), int(m[3]))


def in_window(d: str | None, start: str | None,
              end: str | None) -> bool | None:
    """Three-valued window test. None when the candidate date is unknown
    (caller decides whether unknown passes — for ingestion it does)."""
    nd = norm_date(d)
    if nd is None:
        return None
    s = norm_date(start)
    e = norm_date(end)
    if s and nd < s:
        return False
    if e and nd > e:
        return False
    return True


def passes_window(d: str | None, start: str | None, end: str | None) -> bool:
    """in_window with unknown-passes semantics (the ingestion default)."""
    verdict = in_window(d, start, end)
    return verdict is not False  # True or None(unknown) both pass


def daterange(start: str, end: str, *, cap: int = MAX_RANGE_DAYS) -> list[str]:
    """Inclusive list of ISO dates from start..end (clamped to `cap` days).
    Empty when either bound is unparseable or start > end."""
    s = norm_date(start)
    e = norm_date(end)
    if not s or not e:
        return []
    d0 = date.fromisoformat(s)
    d1 = date.fromisoformat(e)
    if d1 < d0:
        return []
    out: list[str] = []
    cur = d0
    while cur <= d1 and len(out) < cap:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def dedup_candidates(cands: list[BackfillCandidate]) -> list[BackfillCandidate]:
    """First occurrence per URL wins; order preserved."""
    seen: set[str] = set()
    out: list[BackfillCandidate] = []
    for c in cands:
        if not c.url or c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)
    return out
