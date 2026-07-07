"""Frozen Pydantic v2 contracts — everything the API serves plus ingest
inputs. These are the cross-layer contracts (log_buster's schema.py role):
every other layer codes against these, never against raw rows.

All ids are integers (INTEGER PRIMARY KEY). All timestamps are ISO-8601 UTC
strings (storage owns the clock).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from connect.domain.enums import (
    BriefObjectType,
    BriefSection,
    CardTemplate,
    ChannelPlatform,
    DocumentOrigin,
    EnrichmentStatus,
    HeadlineAlign,
    HeadlineSize,
    LinkStatus,
    MediaType,
    PhotoStyle,
    PositionShiftStatus,
    Role,
    SourceType,
    VectorBackend,
    Visibility,
    WatchKind,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- auth / tenancy (v0.2 Phase B) --------------------------------------------

class CurrentUser(_Frozen):
    """The authenticated requester — what GET /api/me serves and what
    get_current_user hands every router."""
    id: int
    email: str
    name: str | None = None
    avatar_url: str | None = None
    role: Role
    created_at: str
    last_login_at: str | None = None


class AuthMethods(_Frozen):
    """GET /api/auth/methods — which sign-in paths this deployment offers."""
    google: bool
    dev: bool


class Invite(_Frozen):
    email: str
    invited_by: int | None = None
    note: str | None = None
    created_at: str


class InviteCreate(_Frozen):
    email: str
    note: str | None = None


# --- admin surface (v0.2 Phase D — tenancy design §5) ---------------------------

class AdminUser(_Frozen):
    """GET /api/admin/users row — app_user incl. the budget-override
    columns (NULL override = role default: members from app_setting,
    admins the purpose envelope)."""
    id: int
    email: str
    name: str | None = None
    avatar_url: str | None = None
    role: Role
    disabled: bool = False
    daily_budget_usd: float | None = None
    investigation_daily_budget_usd: float | None = None
    created_at: str
    last_login_at: str | None = None


class AdminUserUpdate(_Frozen):
    """PATCH /api/admin/users/{id} — absent fields are untouched; an
    EXPLICIT null budget clears the override back to the role default
    (model_fields_set distinguishes the two)."""
    role: Role | None = None
    disabled: bool | None = None
    daily_budget_usd: float | None = Field(default=None, ge=0)
    investigation_daily_budget_usd: float | None = Field(default=None,
                                                         ge=0)


class AdminSettings(_Frozen):
    """GET/PATCH /api/admin/settings — the admin-editable budget globals,
    EFFECTIVE values (app_setting when present, env default otherwise)."""
    global_daily_budget_usd: float
    daily_llm_budget_usd: float
    investigation_daily_budget_usd: float
    member_daily_budget_usd: float
    member_investigation_daily_budget_usd: float


class AdminSettingsUpdate(_Frozen):
    """PATCH body — provided keys are upserted into app_setting (the only
    writer besides first-boot seeding; design §9)."""
    global_daily_budget_usd: float | None = Field(default=None, ge=0)
    daily_llm_budget_usd: float | None = Field(default=None, ge=0)
    investigation_daily_budget_usd: float | None = Field(default=None,
                                                         ge=0)
    member_daily_budget_usd: float | None = Field(default=None, ge=0)
    member_investigation_daily_budget_usd: float | None = Field(
        default=None, ge=0)


# AdminSpend / AdminSpendUser live next to SpendDay below (definition
# order keeps the forward references resolvable at class-build time).


# --- visibility / sharing (v0.2 Phase C tenancy) -------------------------------

class DocumentVisibilityUpdate(_Frozen):
    """PATCH /api/documents/{id} — owner-only visibility flip."""
    visibility: Visibility


class DossierVisibilityUpdate(_Frozen):
    """PATCH /api/analyses/{id} | /api/investigations/{id}.

    private->shared runs the share cascade (design §1): cited private
    documents the owner owns are auto-shared — but only once the caller
    confirms (confirm_documents=true); the unconfirmed call answers 409
    with the confirmation list. shared->private is always 409 (writeback
    has compounded into the shared graph)."""
    visibility: Visibility
    confirm_documents: bool = False


class ShareDocumentRef(_Frozen):
    """One private document the share cascade would flip to shared."""
    id: int
    title: str | None = None


class DossierVisibilityResult(_Frozen):
    id: int
    visibility: Visibility
    # documents the cascade flipped to shared alongside the dossier
    shared_document_ids: list[int] = Field(default_factory=list)


# --- health ------------------------------------------------------------------

class QueueDepth(_Frozen):
    kind: str
    status: str
    count: int


class SourcePollHealth(_Frozen):
    id: int
    name: str
    last_polled_at: str | None = None
    last_poll_status: str | None = None


class BudgetHealth(_Frozen):
    """Spend today vs the effective global backstop — numbers only, never
    secrets (health is unauthenticated)."""
    global_cap_usd: float
    today_usd: float
    general_today_usd: float
    investigation_today_usd: float


class HealthDeep(_Frozen):
    """GET /api/health?deep=1 (runtime design §8): queue depth, beat
    liveness, poll staleness, blob writability, budget state."""
    pg_ok: bool = True
    queue: list[QueueDepth] = Field(default_factory=list)
    queue_oldest_seconds: float | None = None
    beat_runs: dict[str, str] = Field(default_factory=dict)
    sources: list[SourcePollHealth] = Field(default_factory=list)
    blob_dir_writable: bool = True
    budget: BudgetHealth | None = None


class Health(_Frozen):
    ok: bool = True
    schema_version: int
    db_path: str
    vector_backend: VectorBackend
    deep: HealthDeep | None = None


# --- sources -------------------------------------------------------------------

class Source(_Frozen):
    id: int
    name: str
    type: SourceType
    config: dict[str, Any]
    credibility_tier: int
    enabled: bool
    notes: str | None = None
    t1_exempt: bool = False
    created_at: str
    last_polled_at: str | None = None
    last_poll_status: str | None = None
    reliability_score: float | None = None   # S4 dynamic credibility
    doc_count: int = 0


class TrustSource(_Frozen):
    """One cited source as the reader-facing trust panel shows it."""
    ref: str
    document_id: int | None = None
    source_name: str | None = None
    title: str | None = None
    url: str | None = None
    quote: str | None = None
    credibility_tier: int | None = None
    reliability_score: float | None = None


class TrustReport(_Frozen):
    """The 'why trust this' summary for a published deliverable (S6) — assembled
    from the provenance graph (S1), the editorial gate (S2), corrections (S3),
    and dynamic credibility (S4)."""
    gate_verdict: str | None = None          # pass | flagged | error | None
    flagged_count: int = 0
    confidence: float | None = None          # coarse 0-1 trust signal
    sources: list[TrustSource] = Field(default_factory=list)
    tier_mix: dict[str, int] = Field(default_factory=dict)   # tier -> count
    independent_publishers: int = 0
    balance_score: float = 0.0
    balance_label: str = "unknown"           # broad | moderate | narrow | single
    one_sided: bool = False
    contested: list[dict[str, Any]] = Field(default_factory=list)
    corrections: list[dict[str, Any]] = Field(default_factory=list)
    freshness: str | None = None             # newest source date seen
    ai_disclosure: bool = True


class SourceCreate(_Frozen):
    name: str
    type: SourceType
    config: dict[str, Any] = Field(default_factory=dict)
    credibility_tier: int = Field(ge=1, le=4)
    notes: str | None = None
    enabled: bool = True
    t1_exempt: bool = False


class SourceUpdate(_Frozen):
    name: str | None = None
    config: dict[str, Any] | None = None
    credibility_tier: int | None = Field(default=None, ge=1, le=4)
    notes: str | None = None
    enabled: bool | None = None
    t1_exempt: bool | None = None


class SourceTestRequest(_Frozen):
    type: SourceType
    config: dict[str, Any] = Field(default_factory=dict)


_BACKFILL_METHODS = ("sitemap", "pagination", "wayback", "manual")


class BackfillRequest(_Frozen):
    """POST /sources/{id}/backfill — pull OLDER documents into the corpus via
    one or more discovery strategies (default: all four). Dates are ISO
    'YYYY-MM-DD'; an open bound (None) means unbounded on that side."""
    methods: list[str] = Field(
        default_factory=lambda: list(_BACKFILL_METHODS))
    start_date: str | None = None
    end_date: str | None = None
    limit: int = Field(default=200, ge=1, le=5000)
    max_pages: int = Field(default=10, ge=1, le=100)
    # pagination: explicit URL template containing {page} (else ?page=N)
    page_template: str | None = None
    # sitemap: explicit sitemap roots (else probed from the source domain)
    sitemap_urls: list[str] = Field(default_factory=list)
    # manual: explicit URLs and/or a {date}-templated URL over the range
    manual_urls: list[str] = Field(default_factory=list)
    manual_url_template: str | None = None

    @field_validator("methods")
    @classmethod
    def _valid_methods(cls, v: list[str]) -> list[str]:
        cleaned = [m.strip() for m in v if m and m.strip()]
        bad = [m for m in cleaned if m not in _BACKFILL_METHODS]
        if bad:
            raise ValueError(
                f"unknown backfill method(s) {bad}; "
                f"choose from {list(_BACKFILL_METHODS)}")
        if not cleaned:
            raise ValueError("at least one backfill method is required")
        return cleaned

    @field_validator("start_date", "end_date")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from datetime import date
        try:
            date.fromisoformat(v.strip())
        except ValueError as e:
            raise ValueError(f"date must be ISO YYYY-MM-DD: {e}") from e
        return v.strip()


class SampleItem(_Frozen):
    title: str
    url: str | None = None
    published_at: str | None = None


class SourceTestResult(_Frozen):
    ok: bool
    sample_items: list[SampleItem] = Field(default_factory=list)
    error: str | None = None


class DiscoveredItem(_Frozen):
    """One item found by a source adapter's discover()/sample().

    Adapters that already hold the full content (twitter/telegram — fetching
    the item URL again would be wasted or blocked) populate the pre-fetched
    fields below; the poller then stores via ``pipeline.ingest_prefetched``
    instead of ``pipeline.ingest_url``. ``content_text is None`` means
    "fetch the URL" (the rss path).
    """
    title: str
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None
    # -- pre-fetched content (twitter/telegram adapters) ----------------------
    content_text: str | None = None
    author: str | None = None
    media_type: MediaType | None = None
    raw: bytes | None = None           # provider payload -> the raw blob
    link_urls: tuple[str, ...] = ()    # payload out-links -> document_link rows


# --- documents -----------------------------------------------------------------

class DocumentListItem(_Frozen):
    id: int
    source_id: int | None = None
    source_name: str | None = None
    url: str | None = None
    title: str | None = None
    published_at: str | None = None
    fetched_at: str
    media_type: MediaType
    enrichment_status: EnrichmentStatus
    enrichment_tier: int = 0
    watch_hit: bool = False
    canonical_document_id: int | None = None
    snippet: str | None = None
    # tenancy (v0.2 Phase C): owner NULL = system (polled / public web)
    owner_id: int | None = None
    visibility: Visibility = "shared"
    origin: DocumentOrigin = "polled"
    # enrichment tags (NULL/empty until enriched): the document's classified
    # news type + its topics — surfaced as feed tags.
    news_type: str | None = None
    topics: list[str] = Field(default_factory=list)


class FacetSource(_Frozen):
    id: int
    name: str


class FeedFacets(_Frozen):
    """Distinct filter values present in the viewer-visible corpus."""
    news_types: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    sources: list[FacetSource] = Field(default_factory=list)


class DocumentLink(_Frozen):
    """One in-content link extracted from a document's raw HTML."""
    id: int
    document_id: int
    url: str
    anchor_text: str | None = None
    is_file: bool = False
    is_official: bool = False
    status: LinkStatus = "not_followed"
    resolved_document_id: int | None = None
    error: str | None = None
    created_at: str | None = None


class LinkedFrom(_Frozen):
    """A parent document whose extracted link resolved to this document."""
    document_id: int
    title: str | None = None


# --- enrichment (T1 results on the document detail) ----------------------------

class EnrichmentEntityRef(_Frozen):
    id: int
    name: str
    entity_type: str


class EnrichmentClaimRef(_Frozen):
    id: int
    text: str
    check_worthiness: float


class DocumentEnrichment(_Frozen):
    summary: str
    event_type: str
    topics: list[str] = Field(default_factory=list)
    entities: list[EnrichmentEntityRef] = Field(default_factory=list)
    claims: list[EnrichmentClaimRef] = Field(default_factory=list)
    model: str
    created_at: str


class DocumentEventRef(_Frozen):
    """The event a document is clustered into (Phase 2 T2)."""
    id: int
    title: str


class DocumentStatement(_Frozen):
    """One attributed utterance on the document detail (v9)."""
    id: int
    speaker: EnrichmentEntityRef
    quote: str
    topics: list[str] = Field(default_factory=list)
    position_summary: str | None = None


class Document(DocumentListItem):
    author: str | None = None
    language: str | None = None
    content_text: str
    content_hash: str
    links: list[DocumentLink] = Field(default_factory=list)
    linked_from: list[LinkedFrom] = Field(default_factory=list)
    enrichment: DocumentEnrichment | None = None
    event: DocumentEventRef | None = None
    statements: list[DocumentStatement] = Field(default_factory=list)


class LinkFetchResult(_Frozen):
    """POST /document-links/{id}/fetch — the (possibly updated) link plus the
    resolved document; ``document`` is None when the follow failed."""
    link: DocumentLink
    document: Document | None = None


class DocumentPage(_Frozen):
    items: list[DocumentListItem]
    total: int
    page: int
    page_size: int


class DocumentAskRequest(_Frozen):
    """POST /documents/{id}/ask — a free-text question answered from that one
    document's stored text."""
    question: str = Field(min_length=1, max_length=1000)


class DocumentAnswer(_Frozen):
    """A grounded answer to a question about a single document. Doubles as the
    LLM output schema — the field descriptions ARE the model's instructions."""
    answer: str = Field(
        description="A concise answer to the question, derived ONLY from the"
                    " document text. If the document does not contain the"
                    " answer, say so plainly and set grounded=false.")
    grounded: bool = Field(
        description="true only when the answer is supported by the document"
                    " text; false when the document does not address the"
                    " question.")
    quote: str | None = Field(
        default=None,
        description="A short VERBATIM excerpt from the document that supports"
                    " the answer; null when grounded=false.")


# --- social posts (turn a document's enrichment into an Instagram post) -------

class SocialPost(_Frozen):
    """Instagram-ready content generated from a document's enrichment. Doubles
    as the LLM output schema — the field descriptions ARE the instructions. The
    caption/hashtags are the post text; headline + key_points render onto the
    image card. Everything must be grounded in the source document; never
    invent figures."""
    headline: str = Field(
        description="A short, punchy headline for the image card (<=70 chars),"
                    " factual and grounded in the document.")
    caption: str = Field(
        description="The Instagram caption: an engaging but factual 1-3"
                    " sentence summary of the news, neutral in tone, no"
                    " fabricated numbers. End by crediting the source.")
    hashtags: list[str] = Field(
        default_factory=list,
        description="5-12 relevant hashtags WITHOUT the '#' (e.g. 'RBI',"
                    " 'IndianEconomy').")
    key_points: list[str] = Field(
        default_factory=list,
        description="2-4 very short factual bullet points for the card"
                    " (<=60 chars each), each grounded in the document.")
    source_label: str = Field(
        default="",
        description="Short source credit for the card footer, e.g."
                    " 'Source: RBI' or 'Source: Moneycontrol'.")
    alt_text: str = Field(
        default="",
        description="One-sentence accessibility description of the card.")
    image_query: str = Field(
        default="",
        description="Optional: 2-4 plain words to search the web for a"
                    " background photo that fits the story — a concrete,"
                    " photographable subject (e.g. 'Reserve Bank India"
                    " building'). Empty for a clean themed card.")
    suggested_palette: str | None = Field(
        default=None,
        description="Optional: a palette name that fits the news mood, chosen"
                    " ONLY from the list given in the instructions; null if"
                    " none was offered or none fits.")


class SocialPostDraft(_Frozen):
    """A generated post plus its rendered card (base64 JPEG) for instant
    preview/download."""
    content: SocialPost
    image_b64: str


class SocialPublishRequest(_Frozen):
    content: SocialPost


class InstagramStatus(_Frozen):
    """Whether direct posting is available (tokens + public base URL set)."""
    connected: bool
    account_id: str | None = None


class SocialPublishResult(_Frozen):
    media_id: str
    permalink: str | None = None


_HEX = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"


class PostSettings(_Frozen):
    """How posts are generated AND how the card looks. A global default lives
    in app_setting; a workspace can override any subset (merged at use time).
    The card_* / headline_* / logo_sha fields drive connect/social/card.py."""
    # generation
    tone: str = "neutral, factual, engaging"
    hashtag_count: int = Field(default=8, ge=0, le=30)
    brand_handle: str = ""
    caption_max_chars: int = Field(default=400, ge=50, le=2200)
    default_visibility: Visibility = "shared"
    # card look — colors (any valid #rgb/#rrggbb)
    card_bg: str = Field(default="#0f172a", pattern=_HEX)
    card_text: str = Field(default="#f1f5f9", pattern=_HEX)
    card_muted: str = Field(default="#94a3b8", pattern=_HEX)
    card_accent: str = Field(default="#38bdf8", pattern=_HEX)
    # card look — layout
    card_template: CardTemplate = "classic"
    # how a fetched photo sits on the card: 'poster' (default) is the viral
    # news-page look — full-bleed photo, bottom gradient, bold centred
    # accent-colour caption; 'fitted' keeps the whole image on the solid theme
    # colour (never crops the sides); 'cover' is the legacy scrimmed crop.
    card_photo_style: PhotoStyle = "poster"
    headline_size: HeadlineSize = "m"
    headline_align: HeadlineAlign = "left"
    sign_off: str = "via connect"
    # an uploaded brand logo (sha in the social-logo store); rendered on the
    # card when set. Served at /api/social/logo/{sha}.png.
    logo_sha: str | None = None
    # auto-theming: when on, the card palette (colors + template) is chosen per
    # post from its topic (topic_palettes override, else a default map) and the
    # model's suggested palette — see connect/social/palettes.py. The card_*
    # fields above become the fallback look.
    auto_theme: bool = False
    topic_palettes: dict[str, str] = Field(default_factory=dict)


class PostSettingsUpdate(_Frozen):
    tone: str | None = None
    hashtag_count: int | None = Field(default=None, ge=0, le=30)
    brand_handle: str | None = None
    caption_max_chars: int | None = Field(default=None, ge=50, le=2200)
    default_visibility: Visibility | None = None
    card_bg: str | None = Field(default=None, pattern=_HEX)
    card_text: str | None = Field(default=None, pattern=_HEX)
    card_muted: str | None = Field(default=None, pattern=_HEX)
    card_accent: str | None = Field(default=None, pattern=_HEX)
    card_template: CardTemplate | None = None
    card_photo_style: PhotoStyle | None = None
    headline_size: HeadlineSize | None = None
    headline_align: HeadlineAlign | None = None
    sign_off: str | None = None
    logo_sha: str | None = None
    auto_theme: bool | None = None
    topic_palettes: dict[str, str] | None = None


class SocialDraft(_Frozen):
    """A social-post draft the workspace agent generated and saved. The card
    is served at /api/social/card/{card_sha}.jpg."""
    id: int
    workspace_id: int
    document_id: int | None = None
    content: SocialPost
    card_sha: str
    created_at: str


# --- entities (Phase 1) --------------------------------------------------------

class EntityListItem(_Frozen):
    id: int
    name: str
    entity_type: str
    mention_count: int = 0
    document_count: int = 0
    last_seen_at: str | None = None


class EntityPage(_Frozen):
    items: list[EntityListItem]
    total: int
    page: int
    page_size: int


class EntityInfo(_Frozen):
    id: int
    name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    created_at: str


class TopicCount(_Frozen):
    topic: str
    count: int


class CoOccurringEntity(_Frozen):
    entity: EntityListItem
    together: int
    lift: float


class EntityEventRef(_Frozen):
    """One recent event involving the entity (Phase 2)."""
    id: int
    title: str
    event_type: str
    occurred_on: str | None = None


class EntityDelta(_Frozen):
    """New-since-cursor counts (view_cursor surface='entity')."""
    events: int = 0
    documents: int = 0
    claims: int = 0


class EntityDetail(_Frozen):
    entity: EntityInfo
    mention_count: int = 0
    document_count: int = 0
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    topics: list[TopicCount] = Field(default_factory=list)
    co_occurring: list[CoOccurringEntity] = Field(default_factory=list)
    documents: list[DocumentListItem] = Field(default_factory=list)
    events: list[EntityEventRef] = Field(default_factory=list)
    delta: EntityDelta | None = None
    # v9: the entity has statement rows — show the Views tab
    has_views: bool = False


# --- leader views & position tracking (v9) ---------------------------------------

class EntityViewTopic(_Frozen):
    """One topic the entity has spoken on (the views index row)."""
    topic: str
    statement_count: int = 0
    first_at: str | None = None
    last_at: str | None = None
    shift_count: int = 0
    latest_position: str | None = None


class EntityViews(_Frozen):
    entity: EnrichmentEntityRef
    topics: list[EntityViewTopic] = Field(default_factory=list)


class EvolutionSummary(_Frozen):
    """Cached per-(entity, topic) summary; citations are statement ids."""
    text: str
    citations: list[int] = Field(default_factory=list)
    generated_at: str
    stale: bool = False


class TopicStatement(_Frozen):
    """One statement on the per-topic timeline (newest first)."""
    id: int
    quote: str
    position_summary: str | None = None
    stated_at: str | None = None
    document_id: int
    source_name: str | None = None
    credibility_tier: int | None = None
    url: str | None = None
    title: str | None = None


class PositionShiftRef(_Frozen):
    """One open shift on the per-topic timeline."""
    id: int
    kind: str | None = None
    note: str | None = None
    from_statement_id: int
    to_statement_id: int
    detected_at: str


class TopicViews(_Frozen):
    topic: str
    evolution_summary: EvolutionSummary | None = None
    statements: list[TopicStatement] = Field(default_factory=list)
    shifts: list[PositionShiftRef] = Field(default_factory=list)


class PositionShiftRow(_Frozen):
    """The full position_shift row (dismiss response)."""
    id: int
    entity_id: int
    topic: str
    from_statement_id: int
    to_statement_id: int
    kind: str | None = None
    note: str | None = None
    detected_at: str
    status: PositionShiftStatus = "open"


# --- enrichment sweep ------------------------------------------------------------

class EnrichmentSweepRequest(_Frozen):
    mode: Literal["sync", "batch"]
    limit: int | None = Field(default=None, ge=1)
    # v9: 'statements' backfills docs enriched before t1-v2 (full T1 re-run;
    # idempotent replace). None = the normal pending-docs sweep.
    target: Literal["statements"] | None = None


# --- spend ----------------------------------------------------------------------

class SpendDay(_Frozen):
    day: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class SpendSlice(_Frozen):
    """One ledger slice (mine, or the whole deployment): today's total
    across ALL purposes, the general + investigation caps, daily rollups."""
    today_usd: float
    cap_usd: float | None = None
    investigation_cap_usd: float | None = None
    days: list[SpendDay] = Field(default_factory=list)


class SpendReport(_Frozen):
    """GET /api/spend. The v0.1 global fields stay frozen; Phase D adds
    the per-user view (``mine``) and the deployment context (``global``,
    serialized under that name — ``global`` is a Python keyword)."""
    daily_cap_usd: float
    today_spent_usd: float
    days: list[SpendDay] = Field(default_factory=list)
    mine: SpendSlice | None = None
    global_: SpendSlice | None = Field(default=None,
                                       serialization_alias="global")


class AdminSpendUser(_Frozen):
    """One per-user row of GET /api/admin/spend; user_id None = system
    jobs (polls, nightly sweeps). Caps are the EFFECTIVE ceilings."""
    user_id: int | None = None
    email: str | None = None
    name: str | None = None
    today_usd: float = 0.0
    cap_usd: float | None = None
    investigation_cap_usd: float | None = None
    days: list[SpendDay] = Field(default_factory=list)


class AdminSpend(_Frozen):
    """GET /api/admin/spend — system totals + per-user/day breakdown
    (tenancy design §4: GROUP BY user_id over the one ledger)."""
    global_cap_usd: float
    global_today_usd: float
    days: list[SpendDay] = Field(default_factory=list)
    users: list[AdminSpendUser] = Field(default_factory=list)


class SearchResult(_Frozen):
    documents: list[DocumentListItem]
    entities: list[EntityListItem] = Field(default_factory=list)
    total_documents: int = 0
    total_entities: int = 0
    # back-compat alias of total_documents (pre-Phase-1 clients)
    total: int = 0


# --- ingest inputs ----------------------------------------------------------

class IngestUrl(_Frozen):
    url: str


class IngestText(_Frozen):
    text: str
    title: str | None = None


class IngestResult(_Frozen):
    """Pipeline output: the stored (or pre-existing) document and whether a
    new row was created. `created=False` means an exact content-hash dedup
    hit — the API returns 200 + the existing document in that case."""
    document: Document
    created: bool


# --- watches -----------------------------------------------------------------

class Watch(_Frozen):
    id: int
    kind: WatchKind
    label: str
    query_fts: str | None = None
    entity_id: int | None = None
    promote: bool = True
    muted: bool = False
    last_seen_at: str | None = None
    created_at: str
    user_id: int | None = None
    workspace_id: int | None = None


class WatchCreate(_Frozen):
    kind: WatchKind
    label: str
    query_fts: str | None = None
    entity_id: int | None = None
    promote: bool = True
    muted: bool = False
    workspace_id: int | None = None


class WatchUpdate(_Frozen):
    label: str | None = None
    query_fts: str | None = None
    entity_id: int | None = None
    promote: bool | None = None
    muted: bool | None = None


# --- findings board (v6): user-authored posts, often sourced from news --------

class Post(_Frozen):
    """One user-authored finding/note, optionally tied to the news document it
    came from. Owned + shared/private like a dossier."""
    id: int
    title: str
    body: str
    document_id: int | None = None
    document_title: str | None = None   # joined from the linked document
    workspace_id: int | None = None     # the workspace this finding belongs to
    # v13: a verbatim-verified quote from the cited document (agent findings
    # only — the grounding gate); NULL for human-authored posts.
    quote: str | None = None
    quote_start: int | None = None
    quote_end: int | None = None
    visibility: Visibility = "shared"
    owner_id: int | None = None
    owner_name: str | None = None
    created_at: str
    updated_at: str | None = None


class PostCreate(_Frozen):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)
    document_id: int | None = None
    workspace_id: int | None = None
    visibility: Visibility = "shared"


class PostUpdate(_Frozen):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=20_000)
    visibility: Visibility | None = None


# --- workspaces (v7): a saved lens over the shared corpus ----------------------

class Workspace(_Frozen):
    """A named focus (topics / sources / FTS query) that drives a focused feed
    and groups a user's findings + watches. Owned + shared/private."""
    id: int
    name: str
    description: str = ""
    topics: list[str] = Field(default_factory=list)
    source_ids: list[int] = Field(default_factory=list)
    query_fts: str | None = None
    visibility: Visibility = "shared"
    post_settings: dict[str, Any] = Field(default_factory=dict)
    owner_id: int | None = None
    owner_name: str | None = None
    created_at: str
    updated_at: str | None = None


class WorkspaceCreate(_Frozen):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    topics: list[str] = Field(default_factory=list)
    source_ids: list[int] = Field(default_factory=list)
    query_fts: str | None = None
    visibility: Visibility = "shared"


class WorkspaceUpdate(_Frozen):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    topics: list[str] | None = None
    source_ids: list[int] | None = None
    query_fts: str | None = None
    visibility: Visibility | None = None
    post_settings: dict[str, Any] | None = None


# --- characters + channel bindings (v27) --------------------------------------
# A CHARACTER is the persona a reel/post is written and narrated as. The text
# fields ride into generation prompts verbatim (framing-only, like
# ContentOptions.style), so the length caps double as prompt-injection bounds.

class Character(_Frozen):
    id: int
    name: str
    description: str = ""            # personality
    speaking_style: str = ""         # how they talk — shapes the script voice
    sample_line: str = ""            # an audition line (voice + character preview)
    voice_id: str | None = None      # 'vb:<profile>' or an ElevenLabs voice id
    heygen_avatar_id: str | None = None
    catchphrases: list[str] = Field(default_factory=list)
    sign_off: str = ""
    avatar_sha: str | None = None
    owner_id: int | None = None
    created_at: str
    updated_at: str | None = None


class CharacterCreate(_Frozen):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    speaking_style: str = Field(default="", max_length=500)
    sample_line: str = Field(default="", max_length=200)
    voice_id: str | None = None
    heygen_avatar_id: str | None = None
    catchphrases: list[str] = Field(default_factory=list, max_length=8)
    sign_off: str = Field(default="", max_length=120)
    avatar_sha: str | None = None


class CharacterUpdate(_Frozen):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    speaking_style: str | None = Field(default=None, max_length=500)
    sample_line: str | None = Field(default=None, max_length=200)
    voice_id: str | None = None
    heygen_avatar_id: str | None = None
    catchphrases: list[str] | None = Field(default=None, max_length=8)
    sign_off: str | None = Field(default=None, max_length=120)
    avatar_sha: str | None = None


class WorkspaceChannel(_Frozen):
    """A workspace's publishing channel: the account it acts as plus its default
    voice / character / script-type. ``zapier_webhook_url`` and future creds are
    owner-only (masked/omitted for other viewers by the router)."""
    workspace_id: int
    platform: ChannelPlatform = "youtube"
    channel_name: str = ""
    channel_handle: str = ""
    external_id: str | None = None
    zapier_webhook_url: str | None = None
    webhook_set: bool = False        # set even when the URL is masked out
    default_voice_id: str | None = None
    default_character_id: int | None = None
    default_script_type: str | None = None
    has_credentials: bool = False    # reserved: future YouTube OAuth
    created_at: str | None = None
    updated_at: str | None = None


class WorkspaceChannelUpdate(_Frozen):
    platform: ChannelPlatform | None = None
    channel_name: str | None = Field(default=None, max_length=200)
    channel_handle: str | None = Field(default=None, max_length=120)
    external_id: str | None = Field(default=None, max_length=200)
    zapier_webhook_url: str | None = Field(default=None, max_length=600)
    default_voice_id: str | None = Field(default=None, max_length=200)
    default_character_id: int | None = None
    default_script_type: str | None = Field(default=None, max_length=120)


class ChannelSettings(_Frozen):
    """Global channel defaults (app_setting 'channel_settings'), the floor under
    per-workspace channel bindings — mirrors PostSettings' global/workspace
    layering but for voice/character/script rather than card look."""
    default_voice_id: str | None = None
    default_character_id: int | None = None
    default_script_type: str | None = None


class ChannelSettingsUpdate(_Frozen):
    default_voice_id: str | None = Field(default=None, max_length=200)
    default_character_id: int | None = None
    default_script_type: str | None = Field(default=None, max_length=120)


# --- workspace agent (chat) ----------------------------------------------------

class ChatTurn(_Frozen):
    """One human-readable conversation turn (what the UI renders). The
    provider-shaped wire messages that resume the agent are kept server-side,
    not exposed here."""
    role: str
    text: str
    tools_used: list[str] = Field(default_factory=list)
    finding_id: int | None = None
    finding_title: str | None = None


class WorkspaceSourceOption(_Frozen):
    id: int
    name: str
    doc_count: int = 0


class WorkspaceSourceSuggestions(_Frozen):
    """Sources for managing a workspace's focus: those already in focus, plus
    topic-matched candidates (sources publishing on the workspace's topics)."""
    current: list[WorkspaceSourceOption] = Field(default_factory=list)
    suggestions: list[WorkspaceSourceOption] = Field(default_factory=list)


class WorkspaceChatRequest(_Frozen):
    """Send a message to a workspace's agent. Omit ``chat_id`` to start a new
    conversation; pass it to continue a saved one."""
    chat_id: int | None = None
    message: str = Field(min_length=1, max_length=4000)


class WorkspaceChatResponse(_Frozen):
    chat_id: int
    reply: str
    transcript: list[ChatTurn]
    tools_used: list[str] = Field(default_factory=list)
    finding: Post | None = None
    turns_completed: int
    spent_usd: float
    budget_remaining_usd: float


class WorkspaceChatSummary(_Frozen):
    id: int
    title: str
    created_at: str
    updated_at: str | None = None


class WorkspaceChatDetail(_Frozen):
    id: int
    title: str
    transcript: list[ChatTurn]
    created_at: str
    updated_at: str | None = None


# --- async ("deep") workspace-agent runs ---------------------------------------
# A deep run is just a chat turn executed as a background job: the SAME agent as
# the in-request chat, with higher iteration/spend caps. Progress streams over
# job_event (the shared SSE contract) and the answer lands in the chat
# transcript on completion — there is no separate task row, so the job's own
# terminal state is the single source of truth.

class WorkspaceChatAsyncAccepted(_Frozen):
    chat_id: int
    job_id: int


# --- jobs --------------------------------------------------------------------

class Job(_Frozen):
    id: int
    kind: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class JobAccepted(_Frozen):
    job_id: int


# --- events / story threads (Phase 2) -----------------------------------------

class EventInfo(_Frozen):
    id: int
    title: str
    summary: str | None = None
    event_type: str
    occurred_on: str | None = None
    doc_count: int = 0
    story_id: int | None = None
    geo_scope: str | None = None


class EventDetail(_Frozen):
    event: EventInfo
    documents: list[DocumentListItem] = Field(default_factory=list)
    entities: list[EnrichmentEntityRef] = Field(default_factory=list)


class ThreadEventRef(_Frozen):
    id: int
    title: str
    event_type: str
    occurred_on: str | None = None
    doc_count: int = 0


class StoryInfo(_Frozen):
    id: int
    title: str | None = None
    status: str
    doc_count: int = 0
    updated_at: str | None = None


class ThreadDetail(_Frozen):
    story: StoryInfo
    events: list[ThreadEventRef] = Field(default_factory=list)
    entities: list[EnrichmentEntityRef] = Field(default_factory=list)
    summary: str | None = None


# --- Today brief (Phase 2) ------------------------------------------------------

class BriefItem(_Frozen):
    id: int
    section: BriefSection
    rank: int
    object_type: BriefObjectType
    object_id: int
    # template-rendered from score components at generation time — NEVER LLM
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    seen: bool = False


class BriefSections(_Frozen):
    """All six keys always present (empty until their phase fills them)."""
    watch_dev: list[BriefItem] = Field(default_factory=list)
    thread_move: list[BriefItem] = Field(default_factory=list)
    contradiction: list[BriefItem] = Field(default_factory=list)
    trending_claim: list[BriefItem] = Field(default_factory=list)
    suggestion: list[BriefItem] = Field(default_factory=list)
    position_shift: list[BriefItem] = Field(default_factory=list)


class BriefInfo(_Frozen):
    id: int
    brief_date: str
    generated_at: str


class BriefResponse(_Frozen):
    brief: BriefInfo
    sections: BriefSections


# --- analyses (Phase 3 verification slice) ---------------------------------------

AnalysisStatus = Literal["pending", "running", "completed", "failed",
                         "cancelled"]


class AnalysisOptions(_Frozen):
    max_evidence_per_claim: int | None = Field(default=None, ge=1, le=20)


class AnalysisCreate(_Frozen):
    input_text: str
    options: AnalysisOptions = Field(default_factory=AnalysisOptions)
    # None -> the design §1 default: 'shared' (community compounding);
    # 'private' is the explicit opt-in toggle at creation.
    visibility: Visibility | None = None


class AnalysisAccepted(_Frozen):
    analysis_id: int
    job_id: int


class AnalysisVerdictSummary(_Frozen):
    supported: int = 0
    refuted: int = 0
    mixed: int = 0
    unverified: int = 0


class AnalysisListItem(_Frozen):
    id: int
    status: AnalysisStatus
    input_text: str
    created_at: str
    finished_at: str | None = None
    verdict_summary: AnalysisVerdictSummary | None = None
    visibility: Visibility = "shared"
    owner_id: int | None = None
    owner_name: str | None = None


class AnalysisPage(_Frozen):
    items: list[AnalysisListItem]
    total: int
    page: int
    page_size: int


class AnalysisStageInfo(_Frozen):
    stage: Literal["normalize", "verify", "assemble"]
    status: str
    summary: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class AnalysisEvidenceItem(_Frozen):
    id: int                       # evidence table row id
    document_id: int
    source_name: str | None = None
    credibility_tier: int | None = None
    stance: str
    confidence: float | None = None
    quote: str | None = None
    url: str | None = None
    title: str | None = None


class AnalysisClaimItem(_Frozen):
    id: int                       # canonical claim row id (reconciled)
    text: str
    kind: str
    checkable: bool
    verdict: Literal["supported", "refuted", "mixed", "unverified"] | None \
        = None
    confidence: float | None = None
    reasoning: str | None = None
    evidence: list[AnalysisEvidenceItem] = Field(default_factory=list)


class AnalysisDetail(_Frozen):
    id: int
    status: AnalysisStatus
    input_text: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    stages: list[AnalysisStageInfo] = Field(default_factory=list)
    claims: list[AnalysisClaimItem] = Field(default_factory=list)
    last_seq: int = 0
    visibility: Visibility = "shared"
    owner_id: int | None = None
    owner_name: str | None = None


# --- contradictions (Phase 3) ------------------------------------------------------


class ContradictionClaimRef(_Frozen):
    id: int
    text: str
    verdict: str


class ContradictionItem(_Frozen):
    id: int
    claim: ContradictionClaimRef
    n_support: int
    n_refute: int
    best_tier_support: int | None = None
    best_tier_refute: int | None = None
    status: Literal["open", "dismissed", "resolved"]
    detected_at: str


class ContradictionPage(_Frozen):
    items: list[ContradictionItem]
    total: int
    page: int
    page_size: int


class ContradictionDetail(ContradictionItem):
    """A contradiction row plus its per-stance evidence quotes (the two-column
    expand view). ``n_support``/``n_refute`` are the materialized counts over
    ALL evidence; ``evidence`` is only what the caller may see (documents
    private to others are omitted), so the visible quote count can be < the
    counts — by design."""
    evidence: list[AnalysisEvidenceItem] = Field(default_factory=list)


# --- view cursors / calendar (Phase 2) ------------------------------------------

class CursorCreate(_Frozen):
    surface: str
    ref_id: int


class CalendarEntry(_Frozen):
    id: int
    kind: str
    scope: str | None = None
    occurs_on: str
    ends_on: str | None = None
    label: str
