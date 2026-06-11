"""Source poller — asyncio lifespan loop over every discovery-capable
source type (rss, twitter, telegram; the registry's POLLABLE_TYPES).

Each cycle RE-READS enabled sources of the poller's adapter types from the
DB (user-added sources go live without restart). Per-source poll interval
comes from the source's config JSON (poll_interval_minutes, default 30);
per-poll and per-day item caps come from source columns with config
defaults. Overflow and already-seen URLs are skipped, never dropped
silently — counters land in source_stats and last_poll_status.

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

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from connect.domain.models import Source
from connect.ingestion.fetcher import FetchError
from connect.ingestion.pipeline import IngestionPipeline
from connect.sources.base import AdapterSkip
from connect.storage import documents as doc_dao
from connect.storage import sources as source_dao

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_MINUTES = 30


class SourcePoller:
    def __init__(self, conn: sqlite3.Connection, *,
                 pipeline: IngestionPipeline,
                 adapters: Mapping[str, Any],
                 tick_seconds: float = 60.0,
                 default_max_per_poll: int = 25,
                 default_max_per_day: int = 200):
        self.conn = conn
        self.pipeline = pipeline
        self.adapters = dict(adapters)  # type -> adapter instance
        self.tick_seconds = tick_seconds
        self.default_max_per_poll = default_max_per_poll
        self.default_max_per_day = default_max_per_day
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="source-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        log.info("source poller started (tick %.0fs, types %s)",
                 self.tick_seconds, sorted(self.adapters))
        while not self._stop.is_set():
            try:
                await self.poll_due_sources()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("poller cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    # -- polling -------------------------------------------------------------------

    async def poll_due_sources(self) -> None:
        for source in source_dao.list_pollable(
                self.conn, tuple(self.adapters)):
            if self._stop.is_set():
                return
            if self._is_due(source):
                await self.poll_source(source)

    def _is_due(self, source: Source) -> bool:
        if source.last_polled_at is None:
            return True
        interval = int(source.config.get(
            "poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES))
        try:
            last = datetime.fromisoformat(
                source.last_polled_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= last + timedelta(minutes=interval)

    async def poll_source(self, source: Source) -> str:
        """One poll cycle for one source; returns the status string stored
        on the row. Used by both the loop and POST /sources/{id}/poll."""
        adapter = self.adapters.get(source.type)
        if adapter is None:
            status = f"error: no adapter for source type {source.type!r}"
            source_dao.set_poll_result(self.conn, source.id, status[:500])
            return status
        try:
            if getattr(adapter, "uses_since", False):
                items = await adapter.discover(
                    source.config, since=source.last_polled_at)
            else:
                items = await adapter.discover(source.config)
        except AdapterSkip as e:
            status = f"skipped: {e}"
            source_dao.set_poll_result(self.conn, source.id, status[:500])
            return status
        except Exception as e:  # noqa: BLE001
            status = f"error: {e}"
            source_dao.set_poll_result(self.conn, source.id, status[:500])
            return status

        max_per_poll = source.config.get("max_items_per_poll") \
            or self.default_max_per_poll
        max_per_day = source.config.get("max_items_per_day") \
            or self.default_max_per_day
        budget_today = max(0, max_per_day - source_dao.docs_today(
            self.conn, source.id))

        new = dups = errors = 0
        for item in items[:max_per_poll]:
            if new >= budget_today:
                break
            if not item.url:
                continue
            if doc_dao.url_exists(self.conn, item.url):
                dups += 1
                continue
            try:
                if item.content_text is not None:
                    # twitter/telegram: content arrived with the item — the
                    # URL is never fetched (x.com would block it anyway).
                    result = await self.pipeline.ingest_prefetched(
                        item, source_id=source.id)
                else:
                    # parse_feed falls back to the URL when an entry has no
                    # title — don't pass that through as a headline. The
                    # feed's pubDate is likewise more trustworthy than page
                    # metadata (PIB pages embed wrong dates — verified), so
                    # it is passed as a hint that overrides the extracted
                    # date.
                    feed_title = item.title if item.title != item.url else None
                    result = await self.pipeline.ingest_url(
                        item.url, source_id=source.id, title=feed_title,
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
        source_dao.set_poll_result(self.conn, source.id, status)
        source_dao.bump_stats(self.conn, source.id, items=new, dups=dups)
        return status
