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
    EnrichmentStatus,
    LinkStatus,
    MediaType,
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


class Document(DocumentListItem):
    author: str | None = None
    language: str | None = None
    content_text: str
    content_hash: str
    links: list[DocumentLink] = Field(default_factory=list)
    linked_from: list[LinkedFrom] = Field(default_factory=list)
    enrichment: DocumentEnrichment | None = None


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


class EntityDetail(_Frozen):
    entity: EntityInfo
    mention_count: int = 0
    document_count: int = 0
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    topics: list[TopicCount] = Field(default_factory=list)
    co_occurring: list[CoOccurringEntity] = Field(default_factory=list)
    documents: list[DocumentListItem] = Field(default_factory=list)


# --- enrichment sweep ------------------------------------------------------------

class EnrichmentSweepRequest(_Frozen):
    mode: Literal["sync", "batch"]
    limit: int | None = Field(default=None, ge=1)


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
