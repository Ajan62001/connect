"""Composition root — the ONLY place wiring happens.

Builds: DB connection (+ schema/migrations), blob store, fetcher, adapters,
embedder + vector index, ingestion pipeline, RSS poller, job runner, seeds.

startup() is synchronous DB work called from the FastAPI lifespan;
start_background()/shutdown() manage the asyncio machinery.
"""

from __future__ import annotations

import logging
import sqlite3

from connect.analysis.pipeline import AnalysisService
from connect.ingestion.blobs import BlobStore
from connect.investigation.runner import InvestigationService
from connect.ingestion.fetcher import Fetcher
from connect.ingestion.pipeline import IngestionPipeline
from connect.ingestion.poller import SourcePoller
from connect.knowledge.embedder import Embedder, FastEmbedEmbedder, NullEmbedder
from connect.knowledge.enrichment.sweep import (
    EnrichmentService,
    is_fact_checker_source,
)
from connect.knowledge.calendar import seed_calendar_events
from connect.knowledge.taxonomy import seed_event_types
from connect.knowledge.vector import VectorIndex, create_vector_index
from connect.llm.anthropic_provider import AnthropicProvider
from connect.llm.batch_runner import AnthropicBatchRunner
from connect.llm.provider import LLMProvider
from connect.llm.spend import INVESTIGATION_PURPOSES, Governor
from connect.llm.tiers import tier_models
from connect.orchestration.config import Settings
from connect.orchestration.jobs import JobRunner
from connect.retrieval.search_client import SearchClient, create_search_client
from connect.sources.adapters.twitter import TwitterAdapter
from connect.sources.registry import POLLABLE_TYPES, SOURCE_ADAPTERS
from connect.sources.seeds import seed_sources
from connect.storage import db as db_mod

log = logging.getLogger(__name__)


class Container:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.conn: sqlite3.Connection | None = None
        self.schema_version: int = 0
        self.blobs = BlobStore(settings.blob_dir)
        self.fetcher = Fetcher(
            per_domain_interval=settings.fetch_per_domain_interval,
            timeout_seconds=settings.fetch_timeout_seconds,
            max_bytes=settings.fetch_max_bytes,
            respect_robots=settings.fetch_respect_robots,
        )
        self.adapters = {
            name: cls(self.fetcher) for name, cls in SOURCE_ADAPTERS.items()}
        # twitterapi.io needs its key; missing key = graceful poll no-op.
        self.adapters["twitter"] = TwitterAdapter(
            self.fetcher, api_key=settings.twitterapi_io_api_key)
        self.embedder: Embedder = (
            FastEmbedEmbedder() if settings.embeddings_enabled else NullEmbedder())
        self.vectors: VectorIndex | None = None
        self.pipeline: IngestionPipeline | None = None
        self.poller: SourcePoller | None = None
        self.jobs: JobRunner | None = None
        # LLM layer — provider/batch runner are None without an API key;
        # everything that needs them degrades cleanly (T0-only).
        self.llm: LLMProvider | None = None
        self.batch_runner: AnthropicBatchRunner | None = None
        if settings.anthropic_api_key:
            self.llm = AnthropicProvider(
                settings.anthropic_api_key,
                tier_models=tier_models(settings))
            self.batch_runner = AnthropicBatchRunner(
                settings.anthropic_api_key)
        self.governor: Governor | None = None
        self.enrichment: EnrichmentService | None = None
        # Phase 3: web search seam (NullSearchClient when keyless —
        # verification degrades to corpus-only) + the analysis service.
        self.search_client: SearchClient = create_search_client(
            settings.tavily_api_key)
        self.analysis: AnalysisService | None = None
        # v8: investigation mode — its OWN daily governor + service.
        self.investigation_governor: Governor | None = None
        self.investigations: InvestigationService | None = None

    # -- lifecycle --------------------------------------------------------------

    def startup(self) -> None:
        """Open DB, migrate, seed, build the pipeline graph. Synchronous."""
        self.conn = db_mod.connect(self.settings.db_path)
        self.schema_version = db_mod.init_db(self.conn)
        seed_sources(self.conn)
        seed_event_types(self.conn)
        seed_calendar_events(self.conn)
        # general governor excludes investigation spend; the investigation
        # governor sees ONLY it — neither budget gates or charges the other
        self.governor = Governor(
            self.conn, self.settings.daily_llm_budget_usd,
            exclude_purposes=INVESTIGATION_PURPOSES)
        self.investigation_governor = Governor(
            self.conn, self.settings.investigation_daily_budget_usd,
            purposes=INVESTIGATION_PURPOSES)
        self.enrichment = EnrichmentService(
            self.conn,
            provider=self.llm,
            batch_runner=self.batch_runner,
            governor=self.governor,
            batch_poll_seconds=self.settings.batch_poll_seconds,
        )
        self.vectors = create_vector_index(
            self.conn, enabled=self.settings.embeddings_enabled)
        fast_path = (
            self._enrich_fast_path
            if self.settings.enrich_fast_path_enabled and self.llm is not None
            else None)
        self.pipeline = IngestionPipeline(
            self.conn,
            fetcher=self.fetcher,
            blobs=self.blobs,
            embedder=self.embedder,
            vectors=self.vectors,
            embeddings_enabled=self.settings.embeddings_enabled,
            dedup_window_days=self.settings.dedup_window_days,
            simhash_max_hamming=self.settings.simhash_max_hamming,
            link_follow_enabled=self.settings.link_follow_enabled,
            link_follow_max_per_doc=self.settings.link_follow_max_per_doc,
            link_max_per_doc=self.settings.link_max_per_doc,
            official_domains=self.settings.link_official_domains,
            enrich_fast_path=fast_path,
        )
        self.poller = SourcePoller(
            self.conn,
            pipeline=self.pipeline,
            adapters={t: self.adapters[t] for t in POLLABLE_TYPES},
            tick_seconds=self.settings.poll_tick_seconds,
            default_max_per_poll=self.settings.max_items_per_poll,
            default_max_per_day=self.settings.max_items_per_day,
        )
        self.jobs = JobRunner(self.conn, self.settings.job_concurrency)
        self.jobs.reconcile_orphans()
        self.analysis = AnalysisService(
            self.conn,
            jobs=self.jobs,
            provider=self.llm,
            governor=self.governor,
            search=self.search_client,
            ingest=self.pipeline,
            embedder=self.embedder,
            vectors=self.vectors,
            budget_usd=self.settings.analysis_budget_usd,
            default_max_evidence=self.settings.analysis_max_evidence,
        )
        self.investigations = InvestigationService(
            self.conn,
            jobs=self.jobs,
            provider=self.llm,
            governor=self.investigation_governor,
            search=self.search_client,
            ingest=self.pipeline,
            embedder=self.embedder,
            vectors=self.vectors,
            default_budget_usd=self.settings.investigation_budget_usd,
            default_max_iterations=self.settings
            .investigation_max_iterations,
            default_max_web_fetches=self.settings
            .investigation_max_web_fetches,
            synthesis_reserve_usd=self.settings
            .investigation_synthesis_reserve_usd,
            synthesis_tier=self.settings.investigation_synthesis_tier,
        )
        log.info("container up: db=%s schema=v%s vectors=%s",
                 self.settings.db_path, self.schema_version,
                 self.vectors.backend)

    def start_background(self) -> None:
        """Start lifespan tasks (requires a running event loop)."""
        if self.settings.poller_enabled and self.poller is not None:
            self.poller.start()

    async def shutdown(self) -> None:
        if self.poller is not None:
            await self.poller.stop()
        if self.jobs is not None:
            await self.jobs.shutdown()
        await self.fetcher.aclose()
        await self.search_client.aclose()
        twitter = self.adapters.get("twitter")
        if twitter is not None and hasattr(twitter, "aclose"):
            await twitter.aclose()
        if self.llm is not None:
            await self.llm.aclose()
        if self.batch_runner is not None:
            await self.batch_runner.aclose()
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    # -- enrichment fast path -------------------------------------------------------

    def _enrich_fast_path(self, document_id: int, source_id: int | None,
                          watch_hit: bool) -> None:
        """Pipeline hook: watch-hit or fact-checker docs jump the nightly
        queue — one 'enrich_t1_sync' job for just this doc (the governor is
        checked inside the job). Never raises into the ingest."""
        if self.jobs is None or self.enrichment is None:
            return
        if not watch_hit and not is_fact_checker_source(self.db, source_id):
            return
        service = self.enrichment

        async def _run():
            return await service.enrich_document(document_id)

        try:
            self.jobs.submit(
                "enrich_t1_sync", {"document_id": document_id}, _run)
        except RuntimeError:  # no running event loop (sync/offline ingest)
            log.debug("fast-path enrich skipped for doc %s: no event loop",
                      document_id)

    # -- typed accessors (post-startup invariants) --------------------------------

    @property
    def db(self) -> sqlite3.Connection:
        assert self.conn is not None, "Container.startup() not called"
        return self.conn

    @property
    def vector_backend(self) -> str:
        return self.vectors.backend if self.vectors is not None else "disabled"
