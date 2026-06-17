"""Source polling — the per-source poll cycle for every discovery-capable
source type (rss, twitter, telegram; the registry's POLLABLE_TYPES).

v0.2 (runtime design §4): the v0.1 lifespan LOOP is gone. The beat leader
(workers/beat.py) re-reads enabled sources each tick (user-added sources
go live without restart), checks ``is_due`` and enqueues one 'poll_source'
job per due source — polls parallelize across workers and one slow feed
can't stall the cycle. ``SourcePoller.poll_source`` (the per-source logic)
moved here intact and is driven by workers/handlers/poll.py and POST
/sources/{id}/poll.

Per-source poll interval comes from the source's config JSON
(poll_interval_minutes, default 30); per-poll and per-day item caps come
from source columns with config defaults. Overflow and already-seen URLs
are skipped, never dropped silently — counters land in source_stats and
last_poll_status.

Adapter dispatch: source.type -> adapters[type]. Adapters that declare
``uses_since = True`` (twitter: since_time epoch cursor; telegram: time
filter) receive source.last_polled_at; rss keeps its no-cursor behavior
(feeds backfill entries with old pubDates — URL dedup is the idempotency
mechanism there). AdapterSkip (e.g. missing provider key) records a clean
'skipped: <reason>' status — never an error, never retried.

Items carrying pre-fetched content (twitter/telegram) are stored via
pipeline.ingest_prefetched; plain URL items go through pipeline.ingest_url
exactly as before.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.domain.models import Source
from connect.ingestion.fetcher import FetchError
from connect.ingestion.pipeline import IngestionPipeline
from connect.sources.base import AdapterSkip
from connect.storage import documents as doc_dao
from connect.storage import sources as source_dao

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_MINUTES = 30

# Global floor on automatic polling cadence: no source is auto-polled more
# often than this regardless of its per-source poll_interval_minutes. Keeps
# total request volume low (politeness + lower block risk — most CDN blocks
# are rate-triggered) while individual sources may still elect to poll LESS
# often (e.g. 360 = 6h). The manual instant-fetch endpoint bypasses is_due
# entirely, so on-demand polls are never throttled by this floor.
MIN_POLL_INTERVAL_MINUTES = 180  # 3 hours


def is_due(source: Source) -> bool:
    """Is this source due for a poll? (beat's per-tick check; ported intact
    from the v0.1 loop's _is_due)."""
    if source.last_polled_at is None:
        return True
    interval = max(MIN_POLL_INTERVAL_MINUTES, int(source.config.get(
        "poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES)))
    try:
        last = datetime.fromisoformat(
            source.last_polled_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= last + timedelta(minutes=interval)


class SourcePoller:
    def __init__(self, pool: AsyncConnectionPool, *,
                 pipeline: IngestionPipeline,
                 adapters: Mapping[str, Any],
                 default_max_per_poll: int = 25,
                 default_max_per_day: int = 200):
        self.pool = pool
        self.pipeline = pipeline
        self.adapters = dict(adapters)  # type -> adapter instance
        self.default_max_per_poll = default_max_per_poll
        self.default_max_per_day = default_max_per_day

    # -- polling -------------------------------------------------------------------

    async def poll_source(self, conn: psycopg.AsyncConnection,
                          source: Source) -> str:
        """One poll cycle for one source; returns the status string stored
        on the row. Used by both the loop and POST /sources/{id}/poll."""
        adapter = self.adapters.get(source.type)
        if adapter is None:
            status = f"error: no adapter for source type {source.type!r}"
            await source_dao.set_poll_result(conn, source.id, status[:500])
            return status
        try:
            if getattr(adapter, "uses_since", False):
                items = await adapter.discover(
                    source.config, since=source.last_polled_at)
            else:
                items = await adapter.discover(source.config)
        except AdapterSkip as e:
            status = f"skipped: {e}"
            await source_dao.set_poll_result(conn, source.id, status[:500])
            return status
        except Exception as e:  # noqa: BLE001
            status = f"error: {e}"
            await source_dao.set_poll_result(conn, source.id, status[:500])
            return status

        max_per_poll = source.config.get("max_items_per_poll") \
            or self.default_max_per_poll
        max_per_day = source.config.get("max_items_per_day") \
            or self.default_max_per_day
        budget_today = max(0, max_per_day - await source_dao.docs_today(
            conn, source.id))

        new = dups = errors = 0
        for item in items[:max_per_poll]:
            if new >= budget_today:
                break
            if not item.url:
                continue
            if await doc_dao.url_exists(conn, item.url):
                dups += 1
                continue
            try:
                if item.content_text is not None:
                    # twitter/telegram: content arrived with the item — the
                    # URL is never fetched (x.com would block it anyway).
                    result = await self.pipeline.ingest_prefetched(
                        conn, item, source_id=source.id)
                else:
                    # parse_feed falls back to the URL when an entry has no
                    # title — don't pass that through as a headline. The
                    # feed's pubDate is likewise more trustworthy than page
                    # metadata (PIB pages embed wrong dates — verified), so
                    # it is passed as a hint that overrides the extracted
                    # date.
                    feed_title = item.title if item.title != item.url else None
                    result = await self.pipeline.ingest_url(
                        conn, item.url, source_id=source.id,
                        title=feed_title,
                        published_at_hint=item.published_at)
                if result.created:
                    new += 1
                else:
                    dups += 1
            except FetchError as e:
                errors += 1
                log.warning("poll %s: fetch failed %s: %s",
                            source.name, item.url, e)
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("poll %s: ingest failed %s", source.name, item.url)

        status = f"ok: {new} new, {dups} dup, {errors} error"
        await source_dao.set_poll_result(conn, source.id, status)
        await source_dao.bump_stats(conn, source.id, items=new, dups=dups)
        return status
