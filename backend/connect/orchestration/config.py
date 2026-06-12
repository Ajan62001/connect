"""Settings — pydantic-settings, env prefix CONNECT_.

Examples:
    CONNECT_DATABASE_URL=postgresql://connect:connect@127.0.0.1:5432/connect \
    CONNECT_POLLER_ENABLED=false CONNECT_EMBEDDINGS_ENABLED=false \
    uvicorn --factory connect.api.main:create_app
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    # Web search backend selection: with a Tavily key we use it; without one
    # we fall back to the keyless DuckDuckGo scraper so investigations can
    # still reach the open web. Set CONNECT_WEB_SEARCH_ENABLED=false to force
    # NullSearchClient (corpus-only) — the default in tests (offline) and for
    # air-gapped deployments.
    web_search_enabled: bool = True
    twitterapi_io_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TWITTERAPI_IO_API_KEY", "CONNECT_TWITTERAPI_IO_API_KEY"),
    )
    # Instagram direct posting (optional): when BOTH are set AND public_base_url
    # is configured, the social-post feature offers "Post to Instagram" via the
    # Graph API; otherwise it falls back to manual download. The token is a
    # long-lived IG Graph API access token for the business/creator account.
    instagram_access_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "INSTAGRAM_ACCESS_TOKEN", "CONNECT_INSTAGRAM_ACCESS_TOKEN"),
    )
    instagram_business_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "INSTAGRAM_BUSINESS_ACCOUNT_ID",
            "CONNECT_INSTAGRAM_BUSINESS_ACCOUNT_ID"),
    )
    # Public origin of THIS backend (e.g. https://connect.example.com).
    # Instagram's servers fetch the rendered card from
    # {public_base_url}/api/social/card/<id>.jpg, so direct posting needs a
    # publicly reachable URL (localhost is unreachable by Instagram).
    public_base_url: str | None = None

    # --- auth (tenancy design §3, Phase B) -----------------------------------
    # Google OAuth client credentials (Google Cloud Console — see README
    # "Google sign-in"). Unset => Google login is unavailable and
    # /api/auth/methods advertises google: false.
    google_oauth_client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_OAUTH_CLIENT_ID", "CONNECT_GOOGLE_OAUTH_CLIENT_ID"),
    )
    google_oauth_client_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_OAUTH_CLIENT_SECRET", "CONNECT_GOOGLE_OAUTH_CLIENT_SECRET"),
    )
    # Signs ONLY the short-lived oauth_state handshake cookie (the app
    # session is an opaque server-side row, not a signed token). Unset => an
    # ephemeral per-process secret: fine for dev, but logins in-flight across
    # a restart fail — set it in any real deployment.
    session_secret: str | None = None
    # Explicit OAuth redirect URI override. Default: derived from the
    # callback request URL (honors --proxy-headers). Set it when the
    # backend's own origin is not the one registered in Google Console,
    # e.g. CONNECT_OAUTH_REDIRECT_URI=http://localhost:8001/api/auth/callback
    oauth_redirect_uri: str | None = None
    # Origin prefix for post-login redirects ("" = same origin, the Caddy
    # topology). The split-port dev flow (frontend :3000, api :8001) sets
    # CONNECT_FRONTEND_ORIGIN=http://localhost:3000 so the callback lands
    # back on the frontend.
    frontend_origin: str = ""
    # Invite gate (default ON): new Google identities must be invited by an
    # admin, be the first user ever (-> admin), or appear in admin_emails.
    open_signup: bool = False
    # Bootstrap override: these emails may always sign in and are created
    # as admins (comma-separated or JSON list in the env).
    admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # DEV-ONLY login escape hatch: when set, POST /api/auth/dev-login signs
    # in as exactly this email without Google (created on first use; invite
    # gate bypassed — setting the env IS the authorization). NEVER set in
    # production; unset, the endpoint refuses to exist (404).
    dev_login_email: str | None = None
    # Sessions: opaque httpOnly cookie, sliding TTL, hard cap from creation.
    session_ttl_days: int = 30
    session_max_days: int = 90
    # Sliding-expiry writes happen at most once per this interval.
    session_touch_seconds: int = 3600
    # Origin-check middleware (CSRF defense-in-depth): state-changing
    # requests carrying an Origin header must match the request's own host
    # or one of these (the Next dev rewrite forwards the browser's :3000
    # Origin while the backend sees its own Host). Add your LAN origin
    # (e.g. http://192.168.1.20:3000) when serving the dev stack over LAN.
    # Comma-separated or JSON list in the env.
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000",
                                 "http://127.0.0.1:3000"])

    @field_validator("admin_emails", "allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept 'a@x.com,b@y.com' or '["a@x.com"]' alongside a real list
        (NoDecode skips pydantic-settings' JSON pass, so the raw env string
        lands here)."""
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                import json
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return v

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
    # §5; enforced in api/limits.py against job.owner_id)
    user_max_interactive: int = 2
    interactive_queue_limit: int = 20
    # per-user API rate limits (tenancy design §4 — the slowapi pick's
    # `limits` engine driven directly; in-memory moving windows, per
    # process: restarts reset them, the PG governors remain the hard cost
    # backstop). SSE endpoints are exempt.
    rate_limit_enabled: bool = True
    rate_limit_read_per_minute: int = 120
    rate_limit_ingest_per_minute: int = 10
    rate_limit_create_per_minute: int = 5
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
