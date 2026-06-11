"""Frozen Pydantic v2 contracts — everything the API serves plus ingest
inputs. These are the cross-layer contracts (log_buster's schema.py role):
every other layer codes against these, never against raw rows.

All ids are integers (INTEGER PRIMARY KEY). All timestamps are ISO-8601 UTC
strings (storage owns the clock).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from connect.domain.enums import (
    BriefObjectType,
    BriefSection,
    EnrichmentStatus,
    LinkStatus,
    MediaType,
    PositionShiftStatus,
    SourceType,
    VectorBackend,
    WatchKind,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- health ------------------------------------------------------------------

class Health(_Frozen):
    ok: bool = True
    schema_version: int
    db_path: str
    vector_backend: VectorBackend


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
    doc_count: int = 0


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


class SpendReport(_Frozen):
    daily_cap_usd: float
    today_spent_usd: float
    days: list[SpendDay] = Field(default_factory=list)


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


class WatchCreate(_Frozen):
    kind: WatchKind
    label: str
    query_fts: str | None = None
    entity_id: int | None = None
    promote: bool = True
    muted: bool = False


class WatchUpdate(_Frozen):
    label: str | None = None
    query_fts: str | None = None
    entity_id: int | None = None
    promote: bool | None = None
    muted: bool | None = None


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
