"""Domain -> source attribution.

When a web document is ingested by an investigation fetch or a historical
backfill, attaching it to the REAL registered source (a tier 1-3 news outlet)
instead of the generic tier-4 "Web (investigation)" sentinel gives the
document proper credibility provenance — which the analysis/fact-check
surfaces rely on (a moneycontrol article should carry moneycontrol's tier,
not "unverified").

A source's domain is read from its config URL (``index_url`` for web_news,
``feed_url`` for rss). Matching is host-suffix based (``www.`` stripped), so
``economy.moneycontrol.com/article`` resolves to a source registered at
``moneycontrol.com``. On ties the MOST credible (lowest tier number) source
wins. Everything here is pure given a prebuilt index, so it unit-tests with
no DB.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

# domain -> (source_id, credibility_tier)
DomainIndex = dict[str, tuple[int, int]]
SourceResolver = Callable[[Any, str], Awaitable[int | None]]


def norm_host(url: str | None) -> str:
    """Lowercased registrable-ish host: strip creds, port, and a leading
    ``www.``. Returns '' when there is no host."""
    if not url:
        return ""
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _source_host(config: dict[str, Any] | None) -> str:
    cfg = config or {}
    return norm_host(cfg.get("index_url") or cfg.get("feed_url"))


def index_from_sources(sources: list[Any]) -> DomainIndex:
    """Build the domain index from Source rows. Most-credible wins per host
    (lowest tier; id breaks ties for determinism)."""
    index: DomainIndex = {}
    for s in sources:
        host = _source_host(getattr(s, "config", None))
        if not host:
            continue
        tier = getattr(s, "credibility_tier", 4) or 4
        cur = index.get(host)
        if cur is None or tier < cur[1] or (tier == cur[1] and s.id < cur[0]):
            index[host] = (s.id, tier)
    return index


def resolve_in_index(index: DomainIndex, url: str) -> int | None:
    """The registered source id whose domain owns ``url`` (exact host, then
    progressively shorter parent domains), or None."""
    host = norm_host(url)
    if not host:
        return None
    if host in index:
        return index[host][0]
    parts = host.split(".")
    # try parent domains: sub.x.com -> x.com (never down to the bare TLD)
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in index:
            return index[parent][0]
    return None


async def build_domain_index(conn: Any) -> DomainIndex:
    """Load all sources and build the domain index (one query)."""
    from connect.storage import sources as source_dao
    return index_from_sources(await source_dao.list_all(conn))


def make_resolver(index: DomainIndex) -> SourceResolver:
    """A ``(conn, url) -> source_id|None`` resolver over a prebuilt index
    (the conn arg is accepted for the SourceResolver shape but unused)."""
    async def _resolver(_conn: Any, url: str) -> int | None:
        return resolve_in_index(index, url)
    return _resolver
