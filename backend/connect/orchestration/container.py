"""Composition root — the ONLY place wiring happens.

Builds: async connection POOL (+ schema/migrations via init_db), blob
store, fetcher, adapters, embedder + vector index, ingestion pipeline,
source poller, job queue, seeds. Importing connect.workers.handlers here
registers every job handler before the first enqueue — the container
itself is the ``services`` object the handlers receive.

v0.2 (design §2): the single shared sqlite3.Connection became
``Container.pool`` (psycopg AsyncConnectionPool). Startup order matters on
a fresh database: ``await pg.init_db(dsn)`` runs the advisory-lock-guarded
create/migrate on a plain connection FIRST (the pgvector adapter cannot
register before CREATE EXTENSION vector exists), then the pool opens.
``startup()`` is async and is awaited from the FastAPI lifespan;
start_background()/shutdown() manage the asyncio machinery.

Runtime FLAVORS (runtime design §1): the graph is shared; only the job
queue + background pieces differ.

- ``api``    — AsyncioJobQueue (enqueue ALSO executes in-process: the
  embedded-worker mode for no-docker dev and tests; its CAS claim makes it
  safe alongside external workers) + the EventBus (one LISTEN connection
  feeding SSE streams). The v0.1 lifespan poll loop is GONE — beat (in
  workers) schedules polls.
- ``worker`` — PgJobQueue (enqueue-only); claim loops/beat/heartbeats are
  driven by workers/main.py, which owns process lifecycle and signals.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.analysis.pipeline import AnalysisService
from connect.auth.oauth import GoogleOAuth
from connect.ingestion.blobs import BlobStore
from connect.investigation.runner import InvestigationService
from connect.content.service import ContentService
from connect.story.service import StoryService
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
from connect.llm.observability import LLMTracer, build_tracer
from connect.llm.provider import LLMProvider
from connect.llm.spend import (
    GENERAL_SCOPE,
    INVESTIGATION_SCOPE,
    Governor,
    TenantGovernor,
)
from connect.llm.tiers import tier_models
from connect.orchestration.bus import EventBus
from connect.orchestration.config import Settings
from connect.retrieval.search_client import SearchClient, create_search_client
from connect.sources.adapters.twitter import TwitterAdapter
from connect.sources.registry import POLLABLE_TYPES, SOURCE_ADAPTERS
from connect.sources.seeds import repair_sources, seed_sources
from connect.storage import app_settings as app_settings_dao
from connect.storage import pg as pg_mod
from connect.workers import handlers as _handlers  # noqa: F401 — registers job handlers
from connect.workers.queue import AsyncioJobQueue, PgJobQueue

log = logging.getLogger(__name__)


class Container:
    def __init__(self, settings: Settings, flavor: str = "api"):
        assert flavor in ("api", "worker"), flavor
        self.settings = settings
        self.flavor = flavor
        self.bus: EventBus | None = None
        self._pool: AsyncConnectionPool | None = None
        self.schema_version: int = 0
        self.blobs = BlobStore(settings.blob_dir)
        # social cards live in a SEPARATE namespace from document blobs so the
        # public card endpoint can never serve a (possibly private) doc blob.
        self.card_store = BlobStore(settings.blob_dir / "social_cards")
        # uploaded brand logos for post cards — a separate sha-keyed namespace,
        # served (public, sha-unguessable) at /api/social/logo/<sha>.png.
        self.logo_store = BlobStore(settings.blob_dir / "social_logos")
        # rendered reel videos — separate namespace again, served (public) at
        # /api/social/reel/<sha>.mp4 so the endpoint never exposes a doc blob.
        self.reel_store = BlobStore(settings.blob_dir / "social_reels")
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
        self.jobs: PgJobQueue | None = None
        # LLM layer — provider/batch runner are None without an API key;
        # everything that needs them degrades cleanly (T0-only).
        self.llm: LLMProvider | None = None
        self.batch_runner: AnthropicBatchRunner | None = None
        # Langfuse tracing — a no-op tracer without keys, so the provider is
        # built the same way whether or not observability is configured.
        self.llm_tracer: LLMTracer = build_tracer(settings)
        if settings.anthropic_api_key:
            self.llm = AnthropicProvider(
                settings.anthropic_api_key,
                tier_models=tier_models(settings),
                tracer=self.llm_tracer)
            self.batch_runner = AnthropicBatchRunner(
                settings.anthropic_api_key)
        self.governor: Governor | None = None
        self.enrichment: EnrichmentService | None = None
        # Phase 3: web search seam — Tavily with a key, else the keyless
        # DuckDuckGo backend so web search works out of the box; Null only
        # when web_search_enabled=False (tests/air-gapped). The analysis +
        # investigation services share this client.
        self.search_client: SearchClient = create_search_client(
            settings.tavily_api_key,
            web_search_enabled=settings.web_search_enabled)
        self.analysis: AnalysisService | None = None
        # v8: investigation mode — its OWN daily governor + service.
        self.investigation_governor: Governor | None = None
        self.investigations: InvestigationService | None = None
        # v15: story mode — grounded narrative synthesis over a fact-set.
        self.stories: StoryService | None = None
        # v16: social content pipeline — campaigns -> review queue -> publish.
        self.content: ContentService | None = None
        # Phase B auth: Google OAuth client — None without credentials
        # (the signin page then offers only the dev hatch, if enabled).
        # Construction is offline (discovery is fetched lazily at first
        # login), so building it here keeps create_app import-cheap.
        self.google_oauth: GoogleOAuth | None = None
        if (settings.google_oauth_client_id
                and settings.google_oauth_client_secret):
            self.google_oauth = GoogleOAuth(
                settings.google_oauth_client_id,
                settings.google_oauth_client_secret)

    # -- lifecycle --------------------------------------------------------------

    async def startup(self) -> None:
        """Migrate (plain conn), open the pool, seed, build the pipeline
        graph."""
        dsn = self.settings.database_url
        self.schema_version = await pg_mod.init_db(dsn)
        pool = pg_mod.create_pool(
            dsn, min_size=self.settings.pool_min_size,
            max_size=self.settings.pool_max_size)
        await pool.open(wait=True)
        self._pool = pool
        # politeness state goes shared the moment the pool exists (same
        # Fetcher seam, PG backing — runtime design §4)
        self.fetcher.bind_pool(pool)
        async with pool.connection() as conn:
            # Built-in NEWS-domain seeds — skipped on a clean-slate instance
            # (CONNECT_SEED_BUILTIN_SOURCES=false), e.g. the research-papers
            # experiment that must not inherit Indian-news sources/taxonomy.
            if self.settings.seed_builtin_sources:
                # repair must precede seed: it may rename a built-in row (e.g.
                # BQ Prime -> NDTV Profit), and seed_sources would otherwise
                # insert the corrected entry as a duplicate before the rename
                # lands.
                await repair_sources(conn)
                await seed_sources(conn)
                await seed_event_types(conn)
                await seed_calendar_events(conn)
            # budget app_settings (member defaults + global backstop):
            # ON CONFLICT DO NOTHING — admin edits survive restarts
            await app_settings_dao.seed_defaults(conn, self.settings)
        # general governor excludes investigation spend; the investigation
        # governor sees ONLY it — neither budget gates or charges the other.
        # Phase D: both are TenantGovernors — the same purpose envelopes
        # PLUS the GLOBAL $10/day backstop and the per-user ceilings
        # (override column > app_setting member default > env), gated on
        # the ambient job owner set by execute_job.
        self.governor = TenantGovernor(pool, self.settings,
                                       scope=GENERAL_SCOPE)
        self.investigation_governor = TenantGovernor(
            pool, self.settings, scope=INVESTIGATION_SCOPE)
        self.enrichment = EnrichmentService(
            provider=self.llm,
            batch_runner=self.batch_runner,
            governor=self.governor,
            batch_poll_seconds=self.settings.batch_poll_seconds,
            tracer=self.llm_tracer,
        )
        self.vectors = create_vector_index(
            enabled=self.settings.embeddings_enabled)
        fast_path = (
            self._enrich_fast_path
            if self.settings.enrich_fast_path_enabled and self.llm is not None
            else None)
        self.pipeline = IngestionPipeline(
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
            pool,
            pipeline=self.pipeline,
            adapters={t: self.adapters[t] for t in POLLABLE_TYPES},
            default_max_per_poll=self.settings.max_items_per_poll,
            default_max_per_day=self.settings.max_items_per_day,
        )
        # NO startup orphan reconcile — wrong with >1 process; the beat
        # leader's heartbeat sweep owns orphan recovery (runtime design §2)
        if self.flavor == "api":
            self.jobs = AsyncioJobQueue(
                pool, services=self,
                concurrency=self.settings.job_concurrency,
                heartbeat_interval_s=self.settings.heartbeat_seconds)
            self.bus = EventBus(dsn)
        else:
            self.jobs = PgJobQueue(pool)
        self.analysis = AnalysisService(
            pool,
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
            pool,
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
        self.stories = StoryService(
            pool,
            jobs=self.jobs,
            provider=self.llm,
            governor=self.governor,
            embedder=self.embedder,
            vectors=self.vectors,
        )
        self.content = ContentService(
            pool,
            jobs=self.jobs,
            provider=self.llm,
            governor=self.governor,
            settings=self.settings,
            card_store=self.card_store,
            logo_store=self.logo_store,
            reel_store=self.reel_store,
            embedder=self.embedder,
            vectors=self.vectors,
        )
        log.info("container up: db=%s schema=v%s vectors=%s",
                 pg_mod.redact_dsn(self.settings.database_url),
                 self.schema_version, self.vectors.backend)

    def start_background(self) -> None:
        """Start lifespan tasks (requires a running event loop). api: the
        SSE event bus. The poll loop is gone — beat (worker side)
        schedules polls; worker background machinery is owned by
        workers/main.py."""
        if self.bus is not None:
            self.bus.start()

    async def shutdown(self) -> None:
        if self.bus is not None:
            await self.bus.stop()
        if isinstance(self.jobs, AsyncioJobQueue):
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
        self.llm_tracer.flush()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- enrichment fast path -------------------------------------------------------

    async def _enrich_fast_path(self, conn: psycopg.AsyncConnection,
                                document_id: int, source_id: int | None,
                                watch_hit: bool) -> None:
        """Pipeline hook: watch-hit or fact-checker docs jump the nightly
        queue — one 'enrich_t1_sync' job for just this doc (the governor is
        checked inside the job). Never raises into the ingest."""
        if self.jobs is None or self.enrichment is None:
            return
        if not watch_hit and not await is_fact_checker_source(conn,
                                                              source_id):
            return
        try:
            await self.jobs.enqueue("enrich_t1_sync",
                                    {"document_id": document_id})
        except RuntimeError:  # no running event loop (sync/offline ingest)
            log.debug("fast-path enrich skipped for doc %s: no event loop",
                      document_id)

    # -- typed accessors (post-startup invariants) --------------------------------

    @property
    def pool(self) -> AsyncConnectionPool:
        assert self._pool is not None, "Container.startup() not called"
        return self._pool

    @property
    def vector_backend(self) -> str:
        return self.vectors.backend if self.vectors is not None else "disabled"
