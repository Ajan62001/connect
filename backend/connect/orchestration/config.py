"""Settings — pydantic-settings, env prefix CONNECT_.

Examples:
    CONNECT_DATABASE_URL=postgresql://connect:connect@127.0.0.1:5432/connect \
    CONNECT_POLLER_ENABLED=false CONNECT_EMBEDDINGS_ENABLED=false \
    uvicorn --factory connect.api.main:create_app
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ directory (this file is backend/connect/orchestration/config.py)
BACKEND_DIR = Path(__file__).resolve().parents[2]

# Official/primary-source domains (suffix match: 'gov.in' covers every
# *.gov.in host). Used to classify extracted links and to prioritize
# auto-follows. Override with CONNECT_LINK_OFFICIAL_DOMAINS (JSON list).
DEFAULT_OFFICIAL_DOMAINS: tuple[str, ...] = (
    "gov.in",
    "nic.in",
    "rbi.org.in",
    "sebi.gov.in",
    "prsindia.org",
    "indiacode.nic.in",
    "egazette.gov.in",
    "eci.gov.in",
    "indiankanoon.org",
    "sansad.in",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONNECT_",
        extra="ignore",
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        protected_namespaces=(),  # allow the model_fast/_balanced/_deep fields
    )

    # provider keys — conventional unprefixed env names accepted alongside CONNECT_*
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "CONNECT_ANTHROPIC_API_KEY"),
    )
    tavily_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TAVILY_API_KEY", "CONNECT_TAVILY_API_KEY"),
    )
    twitterapi_io_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TWITTERAPI_IO_API_KEY", "CONNECT_TWITTERAPI_IO_API_KEY"),
    )

    # storage — PostgreSQL DSN (v0.2; CONNECT_DATABASE_URL)
    database_url: str = "postgresql://connect:connect@127.0.0.1:5432/connect"
    blob_dir: Path = BACKEND_DIR / "data" / "blobs"
    # pool sizing (design v02-postgres-port.md §2): API replica min=2/max=10;
    # worker max = job_concurrency + 3
    pool_min_size: int = 2
    pool_max_size: int = 10

    # background machinery
    # poller_enabled gates the BEAT's due-source scheduling (the v0.1
    # lifespan poll loop is gone — beat enqueues poll_source jobs)
    poller_enabled: bool = True
    max_items_per_poll: int = 25
    max_items_per_day: int = 200
    # embedded (api-flavor) queue concurrency — Phase A semantics kept for
    # dev/tests; worker processes use worker_concurrency below
    job_concurrency: int = 2

    # worker runtime (v0.2 runtime design §2/§4)
    # local total + per-kind claim slots; kinds absent from the map get the
    # default cap. Cluster-wide caps use advisory-lock slots (expensive
    # kinds only).
    worker_concurrency: int = 6
    worker_kind_caps: dict[str, int] = Field(
        default_factory=lambda: {"investigation": 1, "analysis": 2,
                                 "enrich_t1_batch": 1, "poll_source": 4})
    worker_kind_cap_default: int = 4
    worker_cluster_caps: dict[str, int] = Field(
        default_factory=lambda: {"investigation": 3})
    heartbeat_seconds: float = 15.0
    # a running job whose heartbeat is older than this is orphaned (beat
    # sweep: requeue while attempts < max_attempts, else failed)
    job_stale_seconds: float = 90.0
    beat_tick_seconds: float = 60.0
    beat_retry_seconds: float = 30.0
    # nightly enrich_t1_batch + brief pre-gen fire at this UTC hour
    nightly_sweep_utc_hour: int = 21
    # SSE: live pings come via LISTEN/NOTIFY; this bounds staleness when
    # the bus is down (fallback re-query cadence)
    sse_fallback_seconds: float = 5.0

    # T0 enrichment
    embeddings_enabled: bool = True
    dedup_window_days: int = 14
    simhash_max_hamming: int = 3

    # LLM layer (Phase 1)
    # tier -> model routing; override with CONNECT_MODEL_FAST/_BALANCED/_DEEP
    model_fast: str = "claude-haiku-4-5"
    model_balanced: str = "claude-sonnet-4-6"
    model_deep: str = "claude-opus-4-8"
    daily_llm_budget_usd: float = 2.0
    # deployment-wide daily backstop across ALL users + system jobs (locked
    # decision: $10/day). Seeded into app_setting at startup; the layered
    # TenantGovernor (auth workstream, Phase D) enforces it on the queue
    # path — app_setting wins over env once an admin edits it.
    global_daily_budget_usd: float = 10.0
    # member per-user defaults (locked: $0.50 general / $2.00 investigation;
    # admins keep the $2/$10 env defaults above). Seeded into app_setting;
    # per-user override columns on app_user trump both.
    member_daily_budget_usd: float = 0.50
    member_investigation_daily_budget_usd: float = 2.0
    # worker-flavor cap on concurrent Anthropic calls (runtime design §5;
    # wired in the safety/ops phase alongside the governors)
    llm_max_concurrent: int = 4
    # backpressure → 429 on POST /analyses|/investigations (runtime design
    # §5; enforced with auth, when requests carry a user)
    user_max_interactive: int = 2
    interactive_queue_limit: int = 20
    # sync fast-path: enrich watch-hit / fact-checker docs right after ingest
    enrich_fast_path_enabled: bool = True
    # how often an enrich_t1_batch job polls the Message Batch status
    batch_poll_seconds: float = 30.0

    # analysis pipeline (Phase 3)
    # per-analysis USD cap: degrade evidence K, then abort-with-partial
    analysis_budget_usd: float = 0.75
    # K — max NEW web documents fetched per claim (options can override)
    analysis_max_evidence: int = 6

    # investigation mode (v8) — SEPARATE daily envelope, never the $2 general
    # governor (ledger purposes investigation/investigation_t1/
    # investigation_synthesis)
    investigation_daily_budget_usd: float = 10.0
    # per-run default cap + the slice held back for the DEEP synthesis call
    investigation_budget_usd: float = 1.00
    investigation_synthesis_reserve_usd: float = 0.20
    # 'deep' normally; over-reserve runs fall back to 'balanced' regardless
    investigation_synthesis_tier: str = "deep"
    investigation_max_iterations: int = 14
    investigation_max_web_fetches: int = 8

    # link extraction & selective follow (Phase 0.5)
    link_follow_enabled: bool = True
    link_follow_max_per_doc: int = 5
    link_max_per_doc: int = 50
    link_official_domains: list[str] = Field(
        default_factory=lambda: list(DEFAULT_OFFICIAL_DOMAINS))

    # fetcher politeness
    fetch_timeout_seconds: float = 30.0
    fetch_max_bytes: int = 10 * 1024 * 1024
    fetch_per_domain_interval: float = 2.0
    fetch_respect_robots: bool = True
