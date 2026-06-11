"""Back-compat shim — the poller generalized beyond RSS and lives in
connect.ingestion.poller (SourcePoller, adapter-dict dispatch). Old call
sites importing RssPoller with a single rss adapter keep working."""

from __future__ import annotations

from typing import Any

from psycopg_pool import AsyncConnectionPool

from connect.ingestion.poller import (  # noqa: F401 — re-exports
    DEFAULT_POLL_INTERVAL_MINUTES,
    SourcePoller,
)


class RssPoller(SourcePoller):
    """rss-only constructor kept for old call sites: one adapter, keyed
    under 'rss'. New code should use SourcePoller directly."""

    def __init__(self, pool: AsyncConnectionPool, *, pipeline: Any,
                 adapter: Any, **kwargs: Any):
        super().__init__(pool, pipeline=pipeline,
                         adapters={"rss": adapter}, **kwargs)
