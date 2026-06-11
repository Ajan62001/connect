/**
 * Typed client for the connect API (Phase 0 + Phase 1 + Phase 2 + Phase 3).
 *
 * Types are transcribed exactly from the API contract; all ids are
 * integers (SQLite INTEGER PRIMARY KEY). Every call goes through the Next.js
 * rewrite `/api/*` -> FastAPI, so paths here are origin-relative.
 */

/**
 * Base origin used ONLY for the analysis EventSource (SSE).
 *
 * Regular fetches always go through the Next rewrite (`/api/*`). SSE through
 * the rewrite proxy can buffer events in some dev setups, which would freeze
 * the live analysis view. If that happens, point this single constant at the
 * backend directly (e.g. "http://localhost:8001" — requires CORS on the
 * backend); the default "" keeps EventSource on the same-origin rewrite.
 * Overridable per environment via NEXT_PUBLIC_ANALYSIS_EVENTS_BASE.
 */
export const ANALYSIS_EVENTS_BASE =
  process.env.NEXT_PUBLIC_ANALYSIS_EVENTS_BASE ?? "";

// ---------------------------------------------------------------------------
// Contract types
// ---------------------------------------------------------------------------

export type SourceType =
  | "rss"
  | "twitter"
  | "telegram"
  | "scrape"
  | "api"
  | "search"
  | "manual";
export type CredibilityTier = 1 | 2 | 3 | 4;
export type MediaType =
  | "html"
  | "pdf"
  | "text"
  | "tweet"
  | "telegram"
  | "xlsx" // covers legacy .xls too
  | "docx";

export type EnrichmentStatus =
  | "pending"
  | "queued"
  | "done"
  | "failed"
  | "skipped_dup"
  | "skipped_aged";

export interface Health {
  ok: boolean;
  schema_version: number;
  db_path: string;
  vector_backend: "sqlite-vec" | "bruteforce" | "disabled";
}

export interface Source {
  id: number;
  name: string;
  type: SourceType;
  config: Record<string, unknown>;
  credibility_tier: CredibilityTier;
  enabled: boolean;
  notes: string | null;
  t1_exempt: boolean;
  created_at: string;
  last_polled_at: string | null;
  last_poll_status: string | null;
  doc_count: number;
}

export interface SourceCreate {
  name: string;
  type: SourceType;
  config: Record<string, unknown>;
  credibility_tier: CredibilityTier;
  notes?: string | null;
  enabled?: boolean;
  t1_exempt?: boolean;
}

export interface SourceUpdate {
  name?: string;
  config?: Record<string, unknown>;
  credibility_tier?: CredibilityTier;
  enabled?: boolean;
  notes?: string | null;
  t1_exempt?: boolean;
}

export interface SourceTestRequest {
  type: SourceType;
  config: Record<string, unknown>;
}

export interface SampleItem {
  title: string;
  url: string | null;
  published_at: string | null;
}

export interface SourceTestResult {
  ok: boolean;
  sample_items: SampleItem[];
  error: string | null;
}

export interface JobAccepted {
  job_id: number;
}

export interface DocumentListItem {
  id: number;
  source_id: number | null;
  source_name: string | null;
  url: string | null;
  title: string | null;
  published_at: string | null;
  fetched_at: string;
  media_type: MediaType;
  enrichment_status: EnrichmentStatus;
  enrichment_tier: number;
  watch_hit: boolean;
  canonical_document_id: number | null;
  /** FTS5 snippet — non-null only when the listing was filtered with `q`. */
  snippet: string | null;
}

export type DocumentLinkStatus =
  | "not_followed"
  | "pending"
  | "fetched"
  | "failed"
  | "skipped";

/** One in-content link extracted from a document's raw HTML. */
export interface DocumentLink {
  id: number;
  document_id: number;
  url: string;
  anchor_text: string | null;
  is_file: boolean;
  is_official: boolean;
  status: DocumentLinkStatus;
  resolved_document_id: number | null;
  error: string | null;
  created_at: string | null;
}

/** A parent document whose extracted link resolved to this document. */
export interface LinkedFrom {
  document_id: number;
  title: string | null;
}

// --- Phase 1: entities / enrichment ---------------------------------------

/** Mirrors ENTITY_TYPES in backend domain/enums.py. */
export type EntityType =
  | "person"
  | "organization"
  | "ministry"
  | "company"
  | "political_party"
  | "law"
  | "scheme"
  | "policy_instrument"
  | "place"
  | "other";

export interface EntityListItem {
  id: number;
  name: string;
  entity_type: EntityType;
  mention_count: number;
  document_count: number;
  last_seen_at: string | null;
}

/** Minimal entity reference embedded in a document's enrichment. */
export interface EntityRef {
  id: number;
  name: string;
  entity_type: EntityType;
}

export interface EntityTopic {
  topic: string;
  count: number;
}

export interface CoOccurringEntity {
  entity: EntityListItem;
  /** Documents this entity shares with the page entity (always >= 2). */
  together: number;
  /** Co-occurrence lift (normalized against marginal frequencies). */
  lift: number;
}

export interface EntityDetail {
  entity: {
    id: number;
    name: string;
    entity_type: EntityType;
    aliases: string[];
    description: string | null;
    created_at: string;
  };
  mention_count: number;
  document_count: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  topics: EntityTopic[];
  /** Top 20, lift-ordered, min together >= 2. */
  co_occurring: CoOccurringEntity[];
  /** The 10 most recent documents mentioning this entity. */
  documents: DocumentListItem[];
  /** The 10 most recent events involving this entity (Phase 2). */
  events: EntityEventRef[];
  /** Counts since the view cursor; null when no cursor exists (Phase 2). */
  delta: EntityDelta | null;
}

export interface EntityListParams {
  q?: string;
  page?: number;
  page_size?: number;
}

export interface EnrichmentClaim {
  id: number;
  text: string;
  /** 0..1 — how much this claim deserves a fact-check. */
  check_worthiness: number;
}

/** T1 enrichment payload on GET /documents/{id}; null until enriched. */
export interface DocumentEnrichment {
  summary: string;
  event_type: string;
  topics: string[];
  entities: EntityRef[];
  claims: EnrichmentClaim[];
  model: string;
  created_at: string;
}

export interface Document extends DocumentListItem {
  author: string | null;
  language: string | null;
  content_text: string;
  content_hash: string;
  links: DocumentLink[];
  linked_from: LinkedFrom[];
  enrichment: DocumentEnrichment | null;
  /** Event this document clustered into (T2); null until promotion. */
  event: DocumentEventRef | null;
}

// --- Phase 2: events / threads / briefs / cursors / calendar ----------------

/** Minimal event reference hung off a document detail. */
export interface DocumentEventRef {
  id: number;
  title: string;
}

export type BriefSectionKey =
  | "watch_dev"
  | "thread_move"
  | "contradiction"
  | "trending_claim"
  | "suggestion";

export type BriefObjectType =
  | "event"
  | "document"
  | "claim"
  | "contradiction"
  | "thread";

export interface BriefItem {
  id: number;
  section: BriefSectionKey;
  rank: number;
  object_type: BriefObjectType;
  object_id: number;
  /** Template-rendered from score components — never LLM text. */
  reason: string;
  /** Denormalized display fields (title/date/source/counts as needed). */
  payload: Record<string, unknown>;
  seen: boolean;
}

export interface BriefMeta {
  id: number;
  /** 'YYYY-MM-DD'. */
  brief_date: string;
  generated_at: string;
}

/**
 * All five keys are always present (empty arrays fine); contradiction,
 * trending_claim and suggestion stay empty until Phase 3 verification.
 */
export type BriefSections = Record<BriefSectionKey, BriefItem[]>;

export interface Brief {
  brief: BriefMeta;
  sections: BriefSections;
}

export interface EventDetail {
  event: {
    id: number;
    title: string;
    summary: string | null;
    event_type: string;
    occurred_on: string;
    doc_count: number;
    story_id: number | null;
    geo_scope: string | null;
  };
  documents: DocumentListItem[];
  entities: EntityRef[];
}

/** One event row in a story thread's timeline. */
export interface ThreadEvent {
  id: number;
  title: string;
  event_type: string;
  occurred_on: string;
  doc_count: number;
}

export interface ThreadDetail {
  story: {
    id: number;
    title: string;
    status: "active" | "archived";
    doc_count: number;
    updated_at: string;
  };
  /** Chronological. */
  events: ThreadEvent[];
  entities: EntityRef[];
  summary: string | null;
}

/** One of the 10 most recent events on an entity page. */
export interface EntityEventRef {
  id: number;
  title: string;
  event_type: string;
  occurred_on: string;
}

/** New objects since the entity's view cursor; null when no cursor yet. */
export interface EntityDelta {
  events: number;
  documents: number;
  claims: number;
}

export interface CursorCreate {
  surface: string;
  ref_id: number;
}

export interface CalendarEvent {
  id: number;
  kind: string;
  scope: string | null;
  /** 'YYYY-MM-DD'. */
  occurs_on: string;
  ends_on: string | null;
  label: string;
}

/** POST /document-links/{id}/fetch — `document` is null when the follow failed. */
export interface LinkFetchResult {
  link: DocumentLink;
  document: Document | null;
}

// --- Phase 3: analyses (claim verification) ---------------------------------

export type AnalysisStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

/** The three pipeline stages, in execution order. */
export type AnalysisStageName = "normalize" | "verify" | "assemble";

export const ANALYSIS_STAGES: AnalysisStageName[] = [
  "normalize",
  "verify",
  "assemble",
];

export type ClaimVerdict = "supported" | "refuted" | "mixed" | "unverified";

/** Per-document stance from the verification stage (engine StanceJudgment). */
export type EvidenceStance = "supports" | "refutes" | "mixed" | "unrelated";

/** Verdict counts shown as chips on analysis list rows; null while running. */
export interface VerdictSummary {
  supported: number;
  refuted: number;
  mixed: number;
  unverified: number;
}

export interface AnalysisListItem {
  id: number;
  status: AnalysisStatus;
  input_text: string;
  created_at: string;
  finished_at: string | null;
  verdict_summary: VerdictSummary | null;
}

export interface AnalysisStageRun {
  stage: AnalysisStageName;
  status: AnalysisStatus;
  summary: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** One evidence document judged against a claim, with its verbatim quote. */
export interface AnalysisEvidence {
  id: number;
  document_id: number;
  source_name: string | null;
  credibility_tier: number | null;
  stance: EvidenceStance;
  confidence: number;
  quote: string;
  url: string | null;
  title: string | null;
}

export interface AnalysisClaim {
  id: number;
  text: string;
  kind: string;
  checkable: boolean;
  /** Null until the claim's verification finishes (or for uncheckable claims). */
  verdict: ClaimVerdict | null;
  confidence: number | null;
  reasoning: string | null;
  evidence: AnalysisEvidence[];
}

export interface AnalysisDetail {
  id: number;
  status: AnalysisStatus;
  input_text: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  stages: AnalysisStageRun[];
  claims: AnalysisClaim[];
  /** Highest job_event.seq included in this snapshot; SSE resumes after it. */
  last_seq: number;
}

export interface AnalysisCreate {
  input_text: string;
  options?: { max_evidence_per_claim?: number };
}

/** 202 from POST /api/analyses. */
export interface AnalysisAccepted {
  analysis_id: number;
  job_id: number;
}

export interface AnalysisListParams {
  page?: number;
  page_size?: number;
}

/**
 * SSE payloads on GET /api/analyses/{id}/events. Every wire event carries an
 * `id:` line (job_event.seq) used for the `?after=` resume cursor and the
 * out-of-order guard; the payload is the JSON `data:` body.
 */
export type AnalysisEvent =
  | { type: "stage_started"; stage: AnalysisStageName }
  | { type: "stage_progress"; stage: AnalysisStageName; message: string }
  | { type: "stage_completed"; stage: AnalysisStageName; summary: string }
  | { type: "claim_verified"; claim_id: number; verdict: ClaimVerdict }
  | { type: "done" }
  | { type: "error"; message: string };

/** Wire-format event names (the SSE `event:` field), minus `type`. */
export const ANALYSIS_EVENT_NAMES = [
  "stage_started",
  "stage_progress",
  "stage_completed",
  "claim_verified",
  "done",
  "error",
] as const;

/** EventSource URL for an analysis, resuming after `afterSeq`. */
export function analysisEventsUrl(id: number, afterSeq: number): string {
  return `${ANALYSIS_EVENTS_BASE}/api/analyses/${id}/events${qs({ after: afterSeq })}`;
}

// --- Phase 3: contradictions -------------------------------------------------

export type ContradictionStatus = "open" | "dismissed" | "resolved";

export interface ContradictionListItem {
  id: number;
  claim: {
    id: number;
    text: string;
    verdict: ClaimVerdict | null;
  };
  n_support: number;
  n_refute: number;
  best_tier_support: number;
  best_tier_refute: number;
  status: ContradictionStatus;
  detected_at: string;
}

/**
 * Row expand detail — the list row plus the per-stance evidence quotes.
 * (Contract clarification: the list endpoint only carries counts, so the
 * two-column quote view needs GET /api/contradictions/{id}; the page
 * degrades to counts-only when the endpoint is missing.)
 */
export interface ContradictionDetail extends ContradictionListItem {
  evidence: AnalysisEvidence[];
}

export interface ContradictionListParams {
  status?: ContradictionStatus;
  page?: number;
  page_size?: number;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export type SearchKind = "all" | "documents" | "entities";

export interface SearchResult {
  documents: DocumentListItem[];
  entities: EntityListItem[];
  total_documents: number;
  total_entities: number;
}

// --- Phase 1: LLM spend / enrichment sweeps --------------------------------

export interface SpendDay {
  /** 'YYYY-MM-DD' (UTC). */
  day: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface SpendReport {
  daily_cap_usd: number;
  today_spent_usd: number;
  days: SpendDay[];
}

export type SweepMode = "sync" | "batch";

export interface SweepRequest {
  mode: SweepMode;
  limit?: number;
}

export type WatchKind = "entity" | "topic" | "thread" | "claim" | "search";

export interface Watch {
  id: number;
  kind: WatchKind;
  label: string;
  query_fts: string | null;
  entity_id: number | null;
  promote: boolean;
  muted: boolean;
  last_seen_at: string | null;
  created_at: string;
}

export interface WatchCreate {
  kind: WatchKind;
  label: string;
  query_fts?: string | null;
  entity_id?: number | null;
  promote?: boolean;
  muted?: boolean;
}

export interface WatchUpdate {
  label?: string;
  query_fts?: string | null;
  entity_id?: number | null;
  promote?: boolean;
  muted?: boolean;
}

/** `{watch_id: unread_count}` — JSON object keys arrive as strings. */
export type WatchBadges = Record<string, number>;

export interface DocumentListParams {
  q?: string;
  source_id?: number;
  page?: number;
  page_size?: number;
}

export interface FeedParams {
  status?: EnrichmentStatus;
  page?: number;
  page_size?: number;
}

export interface IngestText {
  text: string;
  title?: string;
}

/** 201 = newly stored, 200 = dedup hit returning the existing document. */
export interface IngestResult {
  document: Document;
  deduped: boolean;
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function parseBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function raise(res: Response): Promise<never> {
  const body = await parseBody(res);
  let detail = `Request failed (${res.status})`;
  if (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof (body as { detail: unknown }).detail === "string"
  ) {
    detail = (body as { detail: string }).detail;
  } else if (typeof body === "string" && body.length > 0) {
    detail = body;
  }
  throw new ApiError(res.status, detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "Backend unreachable — is the API server running?");
  }
  if (!res.ok) await raise(res);
  return (await parseBody(res)) as T;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export function getHealth(): Promise<Health> {
  return request<Health>("/api/health");
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

export function listSources(): Promise<Source[]> {
  return request<Source[]>("/api/sources");
}

export function getSource(id: number): Promise<Source> {
  return request<Source>(`/api/sources/${id}`);
}

export function createSource(payload: SourceCreate): Promise<Source> {
  return request<Source>("/api/sources", jsonInit("POST", payload));
}

export function updateSource(id: number, payload: SourceUpdate): Promise<Source> {
  return request<Source>(`/api/sources/${id}`, jsonInit("PATCH", payload));
}

export function deleteSource(id: number): Promise<void> {
  return request<void>(`/api/sources/${id}`, { method: "DELETE" });
}

export function testSource(payload: SourceTestRequest): Promise<SourceTestResult> {
  return request<SourceTestResult>("/api/sources/test", jsonInit("POST", payload));
}

export function pollSource(id: number): Promise<JobAccepted> {
  return request<JobAccepted>(`/api/sources/${id}/poll`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Documents / ingestion
// ---------------------------------------------------------------------------

async function ingest(init: RequestInit): Promise<IngestResult> {
  let res: Response;
  try {
    res = await fetch("/api/documents", init);
  } catch {
    throw new ApiError(0, "Backend unreachable — is the API server running?");
  }
  if (!res.ok) await raise(res);
  const document = (await parseBody(res)) as Document;
  return { document, deduped: res.status === 200 };
}

export function ingestText(payload: IngestText): Promise<IngestResult> {
  return ingest(jsonInit("POST", payload));
}

export function ingestUrl(url: string): Promise<IngestResult> {
  return ingest(jsonInit("POST", { url }));
}

export function ingestFile(file: File): Promise<IngestResult> {
  const form = new FormData();
  form.append("file", file);
  return ingest({ method: "POST", body: form });
}

export function listDocuments(
  params: DocumentListParams = {},
): Promise<Page<DocumentListItem>> {
  return request<Page<DocumentListItem>>(`/api/documents${qs({ ...params })}`);
}

export function getDocument(id: number): Promise<Document> {
  return request<Document>(`/api/documents/${id}`);
}

export function fetchDocumentLink(linkId: number): Promise<LinkFetchResult> {
  return request<LinkFetchResult>(`/api/document-links/${linkId}/fetch`, {
    method: "POST",
  });
}

export function searchCorpus(
  q: string,
  kind: SearchKind = "all",
): Promise<SearchResult> {
  return request<SearchResult>(`/api/search${qs({ q, kind })}`);
}

export function getFeed(params: FeedParams = {}): Promise<Page<DocumentListItem>> {
  return request<Page<DocumentListItem>>(`/api/feed${qs({ ...params })}`);
}

// ---------------------------------------------------------------------------
// Entities (Phase 1)
// ---------------------------------------------------------------------------

export function listEntities(
  params: EntityListParams = {},
): Promise<Page<EntityListItem>> {
  return request<Page<EntityListItem>>(`/api/entities${qs({ ...params })}`);
}

export function getEntity(id: number): Promise<EntityDetail> {
  return request<EntityDetail>(`/api/entities/${id}`);
}

export function listEntityDocuments(
  id: number,
  params: { page?: number; page_size?: number } = {},
): Promise<Page<DocumentListItem>> {
  return request<Page<DocumentListItem>>(
    `/api/entities/${id}/documents${qs({ ...params })}`,
  );
}

// ---------------------------------------------------------------------------
// LLM spend / enrichment sweeps (Phase 1)
// ---------------------------------------------------------------------------

export function getSpend(days = 7): Promise<SpendReport> {
  return request<SpendReport>(`/api/spend${qs({ days })}`);
}

/** 202 — the sweep runs in a background job; sync mode processes inline. */
export function sweepEnrichment(payload: SweepRequest): Promise<JobAccepted> {
  return request<JobAccepted>("/api/enrichment/sweep", jsonInit("POST", payload));
}

// ---------------------------------------------------------------------------
// Briefs / events / threads / cursors / calendar (Phase 2)
// ---------------------------------------------------------------------------

/** Generated lazily on the first GET of the day, then stable all day. */
export function getBriefToday(): Promise<Brief> {
  return request<Brief>("/api/brief/today");
}

/** `date` is 'YYYY-MM-DD'. */
export function getBrief(date: string): Promise<Brief> {
  return request<Brief>(`/api/brief/${date}`);
}

/** 204. */
export function markBriefItemSeen(itemId: number): Promise<void> {
  return request<void>(`/api/brief/items/${itemId}/seen`, { method: "POST" });
}

export function getEvent(id: number): Promise<EventDetail> {
  return request<EventDetail>(`/api/events/${id}`);
}

export function getThread(id: number): Promise<ThreadDetail> {
  return request<ThreadDetail>(`/api/threads/${id}`);
}

/** 204 — upserts the surface's view cursor to now. */
export function postCursor(payload: CursorCreate): Promise<void> {
  return request<void>("/api/cursors", jsonInit("POST", payload));
}

export function getCalendar(days = 120): Promise<CalendarEvent[]> {
  return request<CalendarEvent[]>(`/api/calendar${qs({ days })}`);
}

/** 202 — manual T2 promotion runs as a background job. */
export function promoteDocument(id: number): Promise<JobAccepted> {
  return request<JobAccepted>(`/api/documents/${id}/promote`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// Watches
// ---------------------------------------------------------------------------

export function listWatches(): Promise<Watch[]> {
  return request<Watch[]>("/api/watches");
}

export function createWatch(payload: WatchCreate): Promise<Watch> {
  return request<Watch>("/api/watches", jsonInit("POST", payload));
}

export function updateWatch(id: number, payload: WatchUpdate): Promise<Watch> {
  return request<Watch>(`/api/watches/${id}`, jsonInit("PATCH", payload));
}

export function deleteWatch(id: number): Promise<void> {
  return request<void>(`/api/watches/${id}`, { method: "DELETE" });
}

export function getWatchBadges(): Promise<WatchBadges> {
  return request<WatchBadges>("/api/watches/badges");
}

export function markWatchSeen(id: number): Promise<Watch> {
  return request<Watch>(`/api/watches/${id}/seen`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Analyses (Phase 3)
// ---------------------------------------------------------------------------

/** 202 — the pipeline runs as a background job; follow it via SSE. */
export function createAnalysis(payload: AnalysisCreate): Promise<AnalysisAccepted> {
  return request<AnalysisAccepted>("/api/analyses", jsonInit("POST", payload));
}

export function listAnalyses(
  params: AnalysisListParams = {},
): Promise<Page<AnalysisListItem>> {
  return request<Page<AnalysisListItem>>(`/api/analyses${qs({ ...params })}`);
}

export function getAnalysis(id: number): Promise<AnalysisDetail> {
  return request<AnalysisDetail>(`/api/analyses/${id}`);
}

/** 202 — cancellation is asynchronous; the snapshot flips when it lands. */
export function cancelAnalysis(id: number): Promise<void> {
  return request<void>(`/api/analyses/${id}/cancel`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Contradictions (Phase 3)
// ---------------------------------------------------------------------------

export function listContradictions(
  params: ContradictionListParams = {},
): Promise<Page<ContradictionListItem>> {
  return request<Page<ContradictionListItem>>(
    `/api/contradictions${qs({ ...params })}`,
  );
}

/** Row detail with per-stance evidence quotes (see ContradictionDetail note). */
export function getContradiction(id: number): Promise<ContradictionDetail> {
  return request<ContradictionDetail>(`/api/contradictions/${id}`);
}

/** 200 — returns the updated row. */
export function dismissContradiction(id: number): Promise<ContradictionListItem> {
  return request<ContradictionListItem>(`/api/contradictions/${id}/dismiss`, {
    method: "POST",
  });
}
