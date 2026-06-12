"""Manual backfill: the human supplies the historical URLs directly, or a
date-templated URL pattern that is expanded across a date range.

Both forms are pure (no network) — the orchestrator fetches/ingests the
resulting URLs. Use this when you know a site's archive URL shape (e.g.
``https://site.com/archive/{date}``) or have a hand-collected URL list.
"""

from __future__ import annotations

from collections.abc import Sequence

from connect.sources.backfill.candidates import (
    MAX_RANGE_DAYS,
    BackfillCandidate,
    daterange,
)

DEFAULT_LIMIT = 2000


def expand_manual_urls(
        *, urls: Sequence[str] | None = None,
        url_template: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = DEFAULT_LIMIT) -> list[BackfillCandidate]:
    """Explicit ``urls`` plus, when ``url_template`` contains ``{date}`` and a
    ``start``/``end`` are given, one URL per day in the range (the date is
    substituted as ``YYYY-MM-DD`` and attached as the candidate's
    published_at). Deduped, http(s)-only, capped at ``limit``."""
    seen: set[str] = set()
    out: list[BackfillCandidate] = []

    for u in (urls or []):
        u = (u or "").strip()
        if (u and u not in seen
                and u.startswith(("http://", "https://"))):
            seen.add(u)
            out.append(BackfillCandidate(url=u, via="manual"))
            if len(out) >= limit:
                return out

    if url_template and "{date}" in url_template and start and end:
        for d in daterange(start, end, cap=MAX_RANGE_DAYS):
            u = url_template.replace("{date}", d)
            if (u not in seen
                    and u.startswith(("http://", "https://"))):
                seen.add(u)
                out.append(BackfillCandidate(
                    url=u, published_at=d, via="manual"))
                if len(out) >= limit:
                    break

    return out
