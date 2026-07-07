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

// --- auth (v0.2 Phase B) ----------------------------------------------------

export type Role = "admin" | "member";

/** GET /api/me — the signed-in user. */
export interface Me {
  id: number;
  email: string;
  name: string | null;
  avatar_url: string | null;
  role: Role;
  created_at: string;
  last_login_at: string | null;
}

/** GET /api/auth/methods — which sign-in paths the backend offers. */
export interface AuthMethods {
  google: boolean;
  dev: boolean;
}

// --- tenancy (v0.2 Phase C) -------------------------------------------------

/**
 * Dossier/document visibility (design §1). Optional on every row that carries
 * it so a backend that predates tenancy keeps all surfaces rendering — an
 * absent value simply hides the chips/affordances.
 */
export type Visibility = "private" | "shared";

/** Mirrors DOCUMENT_ORIGINS in backend domain/enums.py. */
export type DocumentOrigin =
  | "polled"
  | "link_follow"
  | "investigation_fetch"
  | "user_url"
  | "user_upload"
  | "user_text";

/** One private document the share cascade would flip to shared (mirrors
 * backend ShareDocumentRef). Listed in the 409 confirmation payload. */
export interface ShareDocumentRef {
  id: number;
  title: string | null;
}

/** 200 from a dossier visibility PATCH (mirrors DossierVisibilityResult). */
export interface DossierVisibilityResult {
  id: number;
  visibility: Visibility;
  /** Documents the cascade flipped to shared alongside the dossier. */
  shared_document_ids: number[];
}

/**
 * Mine | Shared | All listing scope (design §6 — the community feed lives on
 * the existing dossier lists). Serialized as `?scope=` so a backend that
 * implements it filters (and paginates) server-side; until then FastAPI
 * ignores the unknown param and the UI applies the same predicate
 * client-side over each page (see matchesDossierScope).
 */
export type DossierScope = "all" | "mine" | "shared";

/**
 * The client-side half of the scope contract: the predicate a scoped row
 * must satisfy. Applying it on top of a scope-aware backend is a no-op
 * (idempotent filter), so the UI is correct either way.
 */
export function matchesDossierScope(
  row: { visibility?: Visibility; owner_id?: number | null },
  scope: DossierScope,
  meId: number | undefined,
): boolean {
  switch (scope) {
    case "all":
      return true;
    case "mine":
      return meId !== undefined && row.owner_id === meId;
    case "shared":
      // Pre-tenancy rows carry no visibility — treat them as shared (they
      // ARE the shared corpus on a single-user backend).
      return row.visibility !== "private";
  }
}

export type SourceType =
  | "rss"
  | "web_news"
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
  /** Tenancy (Phase C); absent on a pre-tenancy backend. owner_id NULL =
   *  system-owned (polled / public web fetches). */
  visibility?: Visibility;
  origin?: DocumentOrigin;
  owner_id?: number | null;
  /** Enrichment tags (empty/null until enriched): the document's classified
   *  news type + its topics. */
  news_type?: string | null;
  topics?: string[];
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
  /**
   * True when at least one attributed statement exists for this entity
   * (schema v9) — gates the "Views & statements" section. Optional so a
   * pre-v9 backend keeps the page rendering.
   */
  has_views?: boolean;
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
  /** Attributed statements extracted from this document (schema v9);
   *  optional so a pre-v9 backend keeps the page rendering. */
  statements?: DocumentStatement[];
}

/** A grounded answer to a question about one document (POST /documents/{id}/ask). */
export interface DocumentAnswer {
  answer: string;
  /** true only when the answer is supported by the document text. */
  grounded: boolean;
  /** verbatim supporting excerpt; null when grounded is false. */
  quote: string | null;
}

// --- social posts (turn a document's enrichment into an Instagram post) -----

export interface SocialPost {
  headline: string;
  caption: string;
  hashtags: string[];
  key_points: string[];
  source_label: string;
  alt_text: string;
}

export interface SocialPostDraft {
  content: SocialPost;
  /** base64 JPEG of the rendered 1080×1080 card. */
  image_b64: string;
}

export interface InstagramStatus {
  connected: boolean;
  account_id: string | null;
}

export interface SocialPublishResult {
  media_id: string;
  permalink: string | null;
}

export function draftSocialPost(documentId: number): Promise<SocialPostDraft> {
  return request<SocialPostDraft>(
    `/api/social/documents/${documentId}/draft`,
    { method: "POST" },
  );
}

export function getInstagramStatus(): Promise<InstagramStatus> {
  return request<InstagramStatus>("/api/social/instagram/status");
}

export function publishSocialPost(
  documentId: number,
  content: SocialPost,
): Promise<SocialPublishResult> {
  return request<SocialPublishResult>(
    `/api/social/documents/${documentId}/publish`,
    jsonInit("POST", { content }),
  );
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
  | "position_shift"
  | "contradiction"
  | "trending_claim"
  | "suggestion";

export type BriefObjectType =
  | "event"
  | "document"
  | "claim"
  | "contradiction"
  | "thread"
  | "position_shift";

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
 * All six keys are always present (empty arrays fine); contradiction,
 * trending_claim and suggestion stay empty until Phase 3 verification, and
 * position_shift until schema v9 — consumers read each with `?? []` so an
 * older backend that omits a key degrades gracefully.
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
  /** Tenancy (Phase C); absent on a pre-tenancy backend. */
  visibility?: Visibility;
  owner_id?: number | null;
  owner_name?: string | null;
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
  /** Tenancy (Phase C); absent on a pre-tenancy backend. */
  visibility?: Visibility;
  owner_id?: number | null;
  owner_name?: string | null;
}

export interface AnalysisCreate {
  input_text: string;
  options?: { max_evidence_per_claim?: number };
  /** Default 'shared' (design §1) — private is the opt-in. */
  visibility?: Visibility;
}

/** 202 from POST /api/analyses. */
export interface AnalysisAccepted {
  analysis_id: number;
  job_id: number;
}

export interface AnalysisListParams {
  page?: number;
  page_size?: number;
  /** Mine | Shared | All — see DossierScope ("all"/undefined sends nothing). */
  scope?: DossierScope;
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

// --- Leader views & position tracking (schema v9) ----------------------------

export type PositionShiftKind = "shifted" | "reversed";

/** One topic a speaker has attributed statements on (views index row). */
export interface ViewsTopic {
  topic: string;
  statement_count: number;
  first_at: string | null;
  last_at: string | null;
  /** Open position shifts on this topic. */
  shift_count: number;
  /** position_summary of the newest statement; null when none recorded. */
  latest_position: string | null;
}

/** GET /api/entities/{id}/views — topics ordered by statement_count desc. */
export interface EntityViews {
  entity: { id: number; name: string; entity_type: EntityType };
  topics: ViewsTopic[];
}

/** One attributed, span-verified verbatim statement (per-topic timeline). */
export interface Statement {
  id: number;
  quote: string;
  /** Neutral <=140-char paraphrase of the stance; may be null. */
  position_summary: string | null;
  stated_at: string | null;
  document_id: number;
  source_name: string | null;
  credibility_tier: number | null;
  url: string | null;
  title: string | null;
}

/** Detected drift between two of a speaker's statements on one topic. */
export interface PositionShift {
  id: number;
  kind: PositionShiftKind;
  note: string | null;
  /** Older statement (the "was" side). */
  from_statement_id: number;
  /** Newer statement (the "now" side). */
  to_statement_id: number;
  detected_at: string;
}

/**
 * Cached per-(entity, topic) evolution summary. `text` carries [[s<id>]]
 * citation markers into the statement list; `stale: true` means regeneration
 * was blocked (budget governor) and the text may lag the newest statements.
 */
export interface EvolutionSummary {
  text: string;
  /** Statement ids cited by the text's markers. */
  citations: number[];
  generated_at: string;
  stale: boolean;
}

/** GET /api/entities/{id}/views/{topic} — statements newest first. */
export interface TopicViews {
  topic: string;
  evolution_summary: EvolutionSummary | null;
  statements: Statement[];
  shifts: PositionShift[];
}

/** Statement hung off GET /api/documents/{id} (speaker-side projection). */
export interface DocumentStatement {
  id: number;
  speaker: EntityRef;
  quote: string;
  topics: string[];
  position_summary: string | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

// --- Phase 4: investigations (Investigation Mode, schema v8) -----------------

/** Investigations ride the same dossier/job lifecycle as analyses. */
export type InvestigationStatus = AnalysisStatus;

/** The three investigation stages, in execution order. */
export type InvestigationStageName = "scope" | "investigate" | "synthesize";

export const INVESTIGATION_STAGES: InvestigationStageName[] = [
  "scope",
  "investigate",
  "synthesize",
];

export type InvestigationInputType = "topic" | "entity" | "event" | "story";

/** Mirrors QUESTION_TYPES in the backend (design doc §3.1). */
export type QuestionType =
  | "why_now"
  | "why_this_design"
  | "why_not_alternative"
  | "who_pushed"
  | "what_triggered"
  | "why_silent"
  | "who_benefits"
  | "what_next";

export type QuestionStatus = "open" | "partial" | "answered" | "dropped";

/** Mirrors FINDING_KINDS in the backend (design doc §2.2 record_finding). */
export type FindingKind =
  | "reaction"
  | "trigger"
  | "alternative"
  | "actor_motive"
  | "timing"
  | "context"
  | "consequence";

export interface InvestigationCounts {
  findings: number;
  questions_open: number;
  questions_answered: number;
  docs_added: number;
}

export interface InvestigationListItem {
  id: number;
  status: InvestigationStatus;
  input_type: InvestigationInputType;
  /** Display title — the topic text or the resolved seed object's name. */
  title: string | null;
  created_at: string;
  finished_at: string | null;
  cost_usd: number | null;
  /** Null-tolerant: rows render without chips when the backend omits counts. */
  counts: InvestigationCounts | null;
  /** Tenancy (Phase C); absent on a pre-tenancy backend. */
  visibility?: Visibility;
  owner_id?: number | null;
  owner_name?: string | null;
}

export interface InvestigationStageRun {
  stage: InvestigationStageName;
  status: InvestigationStatus;
  summary: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** One verbatim quote backing a finding (finding_evidence row). */
export interface FindingEvidence {
  document_id: number;
  quote: string;
  title?: string | null;
  source_name?: string | null;
  url?: string | null;
}

export interface InvestigationFinding {
  id: number;
  kind: FindingKind;
  text: string;
  /** True = hypothesis (dashed styling everywhere it surfaces). */
  speculation: boolean;
  confidence: number | null;
  question_id: number | null;
  /** Causal edge written by the grounding gate; joins timeline causal chips. */
  edge_id: number | null;
  evidence: FindingEvidence[];
  created_at?: string;
}

export interface InvestigationQuestion {
  id: number;
  qtype: QuestionType;
  text: string;
  status: QuestionStatus;
  priority: number;
  about_type?: "entity" | "event" | "claim" | "document" | "dossier" | null;
  about_id?: number | null;
  answer_summary: string | null;
  answer_finding_ids: number[];
  /** Set by manual recursion (POST /api/questions/{id}/investigate). */
  spawned_dossier_id: number | null;
}

// Section payloads (design doc §5) — all code/DEEP-produced JSON.

export interface TimelineCausalChip {
  edge_id: number;
  relation: string;
  other_id: number;
  other_title: string | null;
  speculation: boolean;
}

export interface TimelineSectionItem {
  event_id?: number | null;
  document_id?: number | null;
  date: string | null;
  title: string;
  event_type?: string | null;
  doc_count?: number | null;
  causal: TimelineCausalChip[];
}

export interface TimelineSection {
  items: TimelineSectionItem[];
}

export interface ChainStep {
  node_type: string; // 'event' | 'entity' | 'document'
  node_id: number;
  title: string;
  relation_to_next?: string | null;
  speculation: boolean;
  finding_id?: number | null;
}

export interface CausalChain {
  steps: ChainStep[];
}

export interface CausalNarrativeSection {
  /** Markdown with [[f#]] citation markers into the findings table. */
  narrative_md: string;
  chains: CausalChain[];
}

export interface ActorsSectionActor {
  entity_id: number | null;
  name: string;
  role: string | null;
  motive_md: string | null;
  finding_ids: number[];
  speculation: boolean;
}

export interface ActorsSection {
  actors: ActorsSectionActor[];
}

export interface AlternativeItem {
  title: string;
  description: string | null;
  finding_ids: number[];
  by_whom_entity_id?: number | null;
}

export interface AlternativesSection {
  items: AlternativeItem[];
  /** Alternatives nobody documented — the open-question surface. */
  unanswered_question_ids: number[];
}

export interface OpenQuestionsSectionItem {
  question_id: number;
  qtype: QuestionType;
  text: string;
  status: QuestionStatus;
  priority: number;
  spawned_dossier_id?: number | null;
}

export interface OpenQuestionsSection {
  items: OpenQuestionsSectionItem[];
}

export interface WatchNextItem {
  text: string;
  question_id?: number | null;
  calendar_event_id?: number | null;
  /** Pre-filled watch payload; +Watch posts it to the existing watch CRUD. */
  watch_suggestion?: Partial<WatchCreate> | null;
}

export interface WatchNextSection {
  items: WatchNextItem[];
}

export type InvestigationSectionKey =
  | "timeline"
  | "causal_narrative"
  | "actors"
  | "alternatives"
  | "open_questions"
  | "watch_next";

export const INVESTIGATION_SECTION_KEYS: InvestigationSectionKey[] = [
  "timeline",
  "causal_narrative",
  "actors",
  "alternatives",
  "open_questions",
  "watch_next",
];

/** Sections appear as their stages complete; absent = not produced yet. */
export interface InvestigationSections {
  timeline?: TimelineSection | null;
  causal_narrative?: CausalNarrativeSection | null;
  actors?: ActorsSection | null;
  alternatives?: AlternativesSection | null;
  open_questions?: OpenQuestionsSection | null;
  watch_next?: WatchNextSection | null;
}

export interface InvestigationDetail {
  id: number;
  status: InvestigationStatus;
  input_type: InvestigationInputType;
  title: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at: string | null;
  error: string | null;
  budget_usd: number | null;
  cost_usd: number | null;
  /** Set when this investigation was spawned from an open question. */
  parent_question_id?: number | null;
  stages: InvestigationStageRun[];
  sections: InvestigationSections;
  questions: InvestigationQuestion[];
  findings: InvestigationFinding[];
  /** Highest job_event.seq included in this snapshot; SSE resumes after it. */
  last_seq: number;
  /** Tenancy (Phase C); absent on a pre-tenancy backend. */
  visibility?: Visibility;
  owner_id?: number | null;
  owner_name?: string | null;
}

/**
 * Exactly one of topic/entity_id/event_id/story_id/question_id must be set
 * (mirrors InvestigationSeed's validator).
 */
export interface InvestigationCreate {
  topic?: string;
  entity_id?: number;
  event_id?: number;
  story_id?: number;
  question_id?: number;
  options?: {
    budget_usd?: number;
    max_iterations?: number;
    max_web_fetches?: number;
  };
  /** Default 'shared' (design §1) — private is the opt-in. */
  visibility?: Visibility;
}

/** 202 from POST /api/investigations and POST /api/questions/{id}/investigate. */
export interface InvestigationAccepted {
  investigation_id: number;
  job_id: number;
}

export interface InvestigationListParams {
  page?: number;
  page_size?: number;
  /** Mine | Shared | All — see DossierScope ("all"/undefined sends nothing). */
  scope?: DossierScope;
}

/**
 * SSE payloads on GET /api/investigations/{id}/events — same wire contract as
 * analyses (`id:` = job_event.seq, `?after=` resume). Thin payloads: events
 * that carry persisted data (findings, questions, sections) trigger a
 * coalesced snapshot refetch instead of patching the cache.
 */
export type InvestigationEvent =
  | { type: "stage_started"; stage: InvestigationStageName }
  | { type: "stage_progress"; stage: InvestigationStageName; message: string }
  | { type: "stage_completed"; stage: InvestigationStageName; summary?: string | null }
  | { type: "iteration"; n: number; tools?: string[]; cost_so_far?: number }
  | {
      type: "finding_recorded";
      finding_id: number;
      kind?: FindingKind;
      speculation?: boolean;
      text?: string;
    }
  | { type: "question_raised"; question_id: number; qtype?: QuestionType; text?: string }
  | { type: "question_resolved"; question_id: number; status?: QuestionStatus }
  | { type: "doc_ingested"; document_id: number; title?: string | null }
  | { type: "section_completed"; section: InvestigationSectionKey }
  | { type: "done" }
  | { type: "error"; message: string };

/** Wire-format event names (the SSE `event:` field), minus `type`. */
export const INVESTIGATION_EVENT_NAMES = [
  "stage_started",
  "stage_progress",
  "stage_completed",
  "iteration",
  "finding_recorded",
  "question_raised",
  "question_resolved",
  "doc_ingested",
  "section_completed",
  "done",
  "error",
] as const;

/** EventSource URL for an investigation, resuming after `afterSeq`. */
export function investigationEventsUrl(id: number, afterSeq: number): string {
  return `${ANALYSIS_EVENTS_BASE}/api/investigations/${id}/events${qs({ after: afterSeq })}`;
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

// --- spend self-view + admin (v0.2 Phase D) ---------------------------------

/**
 * One spend ledger slice (mine or system-wide), normalized client-side.
 * `cap_usd` null = the backend didn't expose a cap for this slice.
 */
export interface SpendSlice {
  today_usd: number;
  cap_usd: number | null;
  /** Investigations run on a separate daily envelope (design §4). */
  investigation_cap_usd: number | null;
  days: SpendDay[];
}

/**
 * Normalized GET /api/spend. Phase D reshapes the endpoint into "my spend
 * plus global context" (design §4: {my_caps, my_today, my_days[]} + global
 * caps/today); the pre-Phase-D backend serves the global-only SpendReport.
 * normalizeSpend() accepts both: `mine` is null against the legacy shape,
 * and consumers fall back to the global slice.
 */
export interface SpendView {
  mine: SpendSlice | null;
  global: SpendSlice | null;
}

/** GET /api/admin/users row (design §5; mirrors app_user). NULL budget
 *  overrides mean "member default from app_setting". */
export interface AdminUser {
  id: number;
  email: string;
  name: string | null;
  avatar_url: string | null;
  role: Role;
  disabled: boolean;
  daily_budget_usd: number | null;
  investigation_daily_budget_usd: number | null;
  created_at: string | null;
  last_login_at: string | null;
}

/** PATCH /api/admin/users/{id} — explicit null clears a budget override. */
export interface AdminUserUpdate {
  role?: Role;
  disabled?: boolean;
  daily_budget_usd?: number | null;
  investigation_daily_budget_usd?: number | null;
}

/** Invite allowlist row (admin-only invites is a locked decision). */
export interface Invite {
  email: string;
  invited_by: number | null;
  note: string | null;
  created_at: string;
}

export interface InviteCreate {
  email: string;
  note?: string | null;
}

/**
 * GET/PATCH /api/admin/settings — the admin-editable budget globals as
 * EFFECTIVE values (app_setting wins over env once an admin edits it).
 * Null = the backend didn't return the key (older backend); it then falls
 * back to its env default server-side.
 */
export interface AdminSettings {
  /** Deployment-wide daily backstop across all users + system jobs. */
  global_daily_budget_usd: number | null;
  /** General-purpose daily envelope — also the admins' general cap. */
  daily_llm_budget_usd: number | null;
  /** Investigation daily envelope — also the admins' investigation cap. */
  investigation_daily_budget_usd: number | null;
  member_daily_budget_usd: number | null;
  member_investigation_daily_budget_usd: number | null;
}

export interface AdminSettingsUpdate {
  global_daily_budget_usd?: number;
  daily_llm_budget_usd?: number;
  investigation_daily_budget_usd?: number;
  member_daily_budget_usd?: number;
  member_investigation_daily_budget_usd?: number;
}

/** Per-user breakdown row of GET /api/admin/spend. user_id null = system
 *  jobs (polling sweeps, batch enrichment — charged only to the global
 *  envelope). Caps are the *effective* ceilings (override > setting > env)
 *  when the backend provides them. */
export interface AdminSpendUser {
  user_id: number | null;
  email: string | null;
  name: string | null;
  today_usd: number;
  cap_usd: number | null;
  investigation_cap_usd: number | null;
  days: SpendDay[];
}

/** Normalized GET /api/admin/spend — system totals + per-user breakdown. */
export interface AdminSpend {
  global_cap_usd: number | null;
  global_today_usd: number;
  days: SpendDay[];
  users: AdminSpendUser[];
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
  workspace_id: number | null;
}

export interface WatchCreate {
  kind: WatchKind;
  label: string;
  query_fts?: string | null;
  entity_id?: number | null;
  promote?: boolean;
  muted?: boolean;
  workspace_id?: number | null;
}

export interface WatchUpdate {
  label?: string;
  query_fts?: string | null;
  entity_id?: number | null;
  promote?: boolean;
  muted?: boolean;
}

// --- findings board (user-authored posts, often sourced from news) ----------

export interface Post {
  id: number;
  title: string;
  body: string;
  /** the news document this finding was posted from, if any. */
  document_id: number | null;
  document_title: string | null;
  /** the workspace this finding belongs to, if any. */
  workspace_id: number | null;
  visibility: Visibility;
  owner_id: number | null;
  owner_name: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface PostCreate {
  title: string;
  body: string;
  document_id?: number | null;
  workspace_id?: number | null;
  visibility?: Visibility;
}

export interface PostUpdate {
  title?: string;
  body?: string;
  visibility?: Visibility;
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
  source_id?: number;
  news_type?: string;
  topic?: string;
  page?: number;
  page_size?: number;
}

export interface FeedFacets {
  news_types: string[];
  topics: string[];
  sources: { id: number; name: string }[];
}

export function getFeedFacets(): Promise<FeedFacets> {
  return request<FeedFacets>("/api/feed/facets");
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
  /** Parsed response body — for errors whose `detail` is structured JSON
   *  (e.g. the share-cascade 409 confirmation list), not just a string. */
  readonly body: unknown;

  constructor(status: number, detail: string, body?: unknown) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.body = body;
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
  // 401 interceptor: the session is gone (expired / revoked / signed out)
  // — every surface is API-backed, so a hard redirect to the signin page
  // is the whole "route guard". The throw below still rejects the caller's
  // promise; the navigation just wins the race.
  if (
    res.status === 401 &&
    typeof window !== "undefined" &&
    window.location.pathname !== "/signin"
  ) {
    window.location.assign("/signin");
  }
  throw new ApiError(res.status, detail, body);
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
// Auth (v0.2 Phase B)
// ---------------------------------------------------------------------------

export function getMe(): Promise<Me> {
  return request<Me>("/api/me");
}

/** Open endpoint — drives which buttons the signin page renders. */
export function getAuthMethods(): Promise<AuthMethods> {
  return request<AuthMethods>("/api/auth/methods");
}

/**
 * DEV-ONLY escape hatch: signs in as the backend's CONNECT_DEV_LOGIN_EMAIL
 * (404 when the hatch is disabled). Takes no input by design.
 */
export function devLogin(): Promise<Me> {
  return request<Me>("/api/auth/dev-login", { method: "POST" });
}

/** 204 — deletes the server-side session and clears the cookie. */
export function logout(): Promise<void> {
  return request<void>("/api/auth/logout", { method: "POST" });
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

/** Ask a grounded question about one document (synchronous single LLM call). */
export function askDocument(id: number, question: string): Promise<DocumentAnswer> {
  return request<DocumentAnswer>(
    `/api/documents/${id}/ask`,
    jsonInit("POST", { question }),
  );
}

export function fetchDocumentLink(linkId: number): Promise<LinkFetchResult> {
  return request<LinkFetchResult>(`/api/document-links/${linkId}/fetch`, {
    method: "POST",
  });
}

/**
 * Owner-only visibility flip on a document (design §5; body mirrors backend
 * DocumentVisibilityUpdate). Sharing is one-way in practice: a shared
 * document becomes enrichment-eligible and compounds into the shared KB,
 * after which the backend refuses the downgrade (409).
 */
export function setDocumentVisibility(
  id: number,
  visibility: Visibility,
): Promise<unknown> {
  return request<unknown>(
    `/api/documents/${id}`,
    jsonInit("PATCH", { visibility }),
  );
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
// Leader views & position tracking (schema v9)
// ---------------------------------------------------------------------------

export function getEntityViews(id: number): Promise<EntityViews> {
  return request<EntityViews>(`/api/entities/${id}/views`);
}

/**
 * Reading a topic's views lazily regenerates a stale evolution summary
 * server-side (governed), so this call can take a few seconds when the
 * summary is being refreshed; fresh reads are instant.
 */
export function getTopicViews(id: number, topic: string): Promise<TopicViews> {
  return request<TopicViews>(
    `/api/entities/${id}/views/${encodeURIComponent(topic)}`,
  );
}

/** 200 — returns the updated row. */
export function dismissPositionShift(id: number): Promise<PositionShift> {
  return request<PositionShift>(`/api/position-shifts/${id}/dismiss`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// LLM spend / enrichment sweeps (Phase 1 + v0.2 Phase D self-view)
// ---------------------------------------------------------------------------

// Phase D reshapes /api/spend into "my spend + global context" and adds the
// /api/admin/* management surface; the exact JSON field names aren't frozen
// yet (the backend lands in a sibling workstream), so everything below
// decodes tolerantly: numbers are picked from a candidate-key list and
// missing pieces degrade to null instead of breaking the page.

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** First finite number found under any of `keys` (app_setting values arrive
 *  as strings — the TEXT column — so numeric strings count). */
function pickNumber(
  record: Record<string, unknown>,
  keys: readonly string[],
): number | null {
  for (const key of keys) {
    const value = asFiniteNumber(record[key]);
    if (value !== null) return value;
  }
  return null;
}

function asSpendDays(value: unknown): SpendDay[] {
  if (!Array.isArray(value)) return [];
  const days: SpendDay[] = [];
  for (const item of value) {
    const record = asRecord(item);
    if (!record || typeof record.day !== "string") continue;
    days.push({
      day: record.day,
      calls: asFiniteNumber(record.calls) ?? 0,
      input_tokens: asFiniteNumber(record.input_tokens) ?? 0,
      output_tokens: asFiniteNumber(record.output_tokens) ?? 0,
      cost_usd: asFiniteNumber(record.cost_usd) ?? 0,
    });
  }
  return days;
}

/** Decodes one SpendSlice from a record using legacy/global key names. */
function decodeSpendSlice(record: Record<string, unknown>): SpendSlice | null {
  const today = pickNumber(record, ["today_spent_usd", "today_usd", "spent_usd"]);
  const cap = pickNumber(record, ["daily_cap_usd", "cap_usd", "daily_budget_usd"]);
  if (today === null && cap === null) return null;
  return {
    today_usd: today ?? 0,
    cap_usd: cap,
    investigation_cap_usd: pickNumber(record, [
      "investigation_cap_usd",
      "investigation_daily_budget_usd",
    ]),
    days: asSpendDays(record.days),
  };
}

/**
 * Accepts every plausible /api/spend shape:
 * - legacy (pre-Phase-D): {daily_cap_usd, today_spent_usd, days} → global only;
 * - design §4 flat: {my_caps{...}, my_today*, my_days, global_*};
 * - nested: {mine: {...}, global: {...}} with legacy key names inside.
 */
export function normalizeSpend(raw: unknown): SpendView {
  const record = asRecord(raw) ?? {};

  const mineNested = asRecord(record.mine ?? record.me);
  const hasFlatMine = ["my_caps", "my_today", "my_today_usd", "my_days"].some(
    (key) => key in record,
  );

  let mine: SpendSlice | null = null;
  if (mineNested) {
    mine = decodeSpendSlice(mineNested);
  } else if (hasFlatMine) {
    const caps = asRecord(record.my_caps) ?? {};
    mine = {
      today_usd:
        pickNumber(record, ["my_today_usd", "my_today", "my_spent_today_usd"]) ?? 0,
      cap_usd:
        pickNumber(caps, ["daily_usd", "daily_budget_usd", "daily_cap_usd", "general_usd"]) ??
        pickNumber(record, ["my_cap_usd", "my_daily_cap_usd"]),
      investigation_cap_usd:
        pickNumber(caps, [
          "investigation_daily_usd",
          "investigation_daily_budget_usd",
          "investigation_usd",
        ]) ?? pickNumber(record, ["my_investigation_cap_usd"]),
      days: asSpendDays(record.my_days),
    };
  }

  const globalNested = asRecord(record.global ?? record.system);
  let global: SpendSlice | null = null;
  if (globalNested) {
    global = decodeSpendSlice(globalNested);
  } else if (mine !== null) {
    // My-spend shape with flat global_* context fields.
    const today = pickNumber(record, ["global_today_usd", "global_today"]);
    const cap = pickNumber(record, ["global_cap_usd", "global_daily_budget_usd"]);
    if (today !== null || cap !== null) {
      global = {
        today_usd: today ?? 0,
        cap_usd: cap,
        investigation_cap_usd: pickNumber(record, ["global_investigation_cap_usd"]),
        days: asSpendDays(record.global_days),
      };
    }
  } else {
    // Legacy single-ledger shape: the one report IS the global view.
    global = decodeSpendSlice(record);
  }

  return { mine, global };
}

/** Normalized spend view — my spend (Phase D) plus the global envelope. */
export async function getSpendView(days = 7): Promise<SpendView> {
  const raw = await request<unknown>(`/api/spend${qs({ days })}`);
  return normalizeSpend(raw);
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

export function enrichDocument(id: number): Promise<JobAccepted> {
  return request<JobAccepted>(`/api/documents/${id}/enrich`, {
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
// Findings board (posts)
// ---------------------------------------------------------------------------

export interface PostFilter {
  document_id?: number;
  workspace_id?: number;
}

export function listPosts(filter: PostFilter = {}): Promise<Post[]> {
  return request<Post[]>(`/api/posts${qs({ ...filter })}`);
}

export function createPost(payload: PostCreate): Promise<Post> {
  return request<Post>("/api/posts", jsonInit("POST", payload));
}

export function updatePost(id: number, payload: PostUpdate): Promise<Post> {
  return request<Post>(`/api/posts/${id}`, jsonInit("PATCH", payload));
}

export function deletePost(id: number): Promise<void> {
  return request<void>(`/api/posts/${id}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Workspaces (a saved lens over the shared corpus)
// ---------------------------------------------------------------------------

export interface Workspace {
  id: number;
  name: string;
  description: string;
  topics: string[];
  source_ids: number[];
  query_fts: string | null;
  visibility: Visibility;
  post_settings: Record<string, unknown>;
  owner_id: number | null;
  owner_name: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface WorkspaceCreate {
  name: string;
  description?: string;
  topics?: string[];
  source_ids?: number[];
  query_fts?: string | null;
  visibility?: Visibility;
}

export interface WorkspaceUpdate {
  name?: string;
  description?: string;
  topics?: string[];
  source_ids?: number[];
  query_fts?: string | null;
  visibility?: Visibility;
  post_settings?: Record<string, unknown>;
}

/** The controlled topic vocabulary a workspace's focus is chosen from. */
export function getTopics(): Promise<string[]> {
  return request<string[]>("/api/topics");
}

/** How posts are generated (global default, overridable per workspace). */
export type CardTemplate = "classic" | "bold" | "minimal";
/** How a fetched photo sits on a card: 'poster' (default) = full-bleed photo
 * + bold bottom accent caption (viral news-page look); 'fitted' = whole image
 * on the solid theme colour; 'cover' = legacy full-bleed scrimmed crop. */
export type PhotoStyle = "poster" | "fitted" | "cover";
export type HeadlineSize = "s" | "m" | "l";
export type HeadlineAlign = "left" | "center";

export interface PostSettings {
  tone: string;
  hashtag_count: number;
  brand_handle: string;
  caption_max_chars: number;
  default_visibility: Visibility;
  card_bg: string;
  card_text: string;
  card_muted: string;
  card_accent: string;
  card_template: CardTemplate;
  card_photo_style: PhotoStyle;
  headline_size: HeadlineSize;
  headline_align: HeadlineAlign;
  sign_off: string;
  logo_sha: string | null;
  auto_theme: boolean;
  topic_palettes: Record<string, string>;
}

export interface Palette {
  card_bg: string;
  card_text: string;
  card_muted: string;
  card_accent: string;
  card_template: CardTemplate;
}

export interface PaletteCatalog {
  palettes: Record<string, Palette>;
  topic_defaults: Record<string, string>;
  topics: string[];
}

export function getPostPalettes(): Promise<PaletteCatalog> {
  return request<PaletteCatalog>("/api/social/palettes");
}

export function getWorkspacePostSettings(id: number): Promise<PostSettings> {
  return request<PostSettings>(`/api/workspaces/${id}/post-settings`);
}

/** Render a sample card with these settings (no LLM); returns an object URL
 * the caller must revoke. Drives the live style preview. */
export async function previewPostCard(settings: PostSettings): Promise<string> {
  let res: Response;
  try {
    res = await fetch("/api/social/preview", jsonInit("POST", settings));
  } catch {
    throw new ApiError(0, "Backend unreachable");
  }
  if (!res.ok) await raise(res);
  return URL.createObjectURL(await res.blob());
}

/** Upload a brand logo; returns its sha for PostSettings.logo_sha. */
export function uploadPostLogo(file: File): Promise<{ logo_sha: string }> {
  const form = new FormData();
  form.append("file", file);
  return request<{ logo_sha: string }>("/api/social/logo", {
    method: "POST",
    body: form,
  });
}

/** A social-post draft the workspace agent generated. */
export interface SocialDraft {
  id: number;
  workspace_id: number;
  document_id: number | null;
  content: SocialPost;
  card_sha: string;
  created_at: string;
}

export function listWorkspaceSocialDrafts(id: number): Promise<SocialDraft[]> {
  return request<SocialDraft[]>(`/api/workspaces/${id}/social-drafts`);
}

export function deleteWorkspaceSocialDraft(
  id: number,
  draftId: number,
): Promise<void> {
  return request<void>(`/api/workspaces/${id}/social-drafts/${draftId}`, {
    method: "DELETE",
  });
}

// --- workspace knowledge base (the workspace's own documents) ---------------

async function ingestToWorkspace(
  id: number,
  init: RequestInit,
): Promise<IngestResult> {
  let res: Response;
  try {
    res = await fetch(`/api/workspaces/${id}/documents`, init);
  } catch {
    throw new ApiError(0, "Backend unreachable — is the API server running?");
  }
  if (!res.ok) await raise(res);
  const document = (await parseBody(res)) as Document;
  return { document, deduped: res.status === 200 };
}

export function addWorkspaceText(
  id: number,
  payload: IngestText,
): Promise<IngestResult> {
  return ingestToWorkspace(id, jsonInit("POST", payload));
}

export function addWorkspaceUrl(id: number, url: string): Promise<IngestResult> {
  return ingestToWorkspace(id, jsonInit("POST", { url }));
}

export function addWorkspaceFile(
  id: number,
  file: File,
): Promise<IngestResult> {
  const form = new FormData();
  form.append("file", file);
  return ingestToWorkspace(id, { method: "POST", body: form });
}

export function listWorkspaceDocuments(
  id: number,
  page = 1,
  pageSize = 20,
): Promise<Page<DocumentListItem>> {
  return request<Page<DocumentListItem>>(
    `/api/workspaces/${id}/documents${qs({ page, page_size: pageSize })}`,
  );
}

// --- async ("deep") workspace-agent runs ------------------------------------
// A deep run is a chat turn executed as a background job: progress streams over
// job_event (the shared SSE contract, same as analyses/investigations) and the
// answer lands in the chat transcript on completion.

export interface WorkspaceChatAsyncAccepted {
  chat_id: number;
  job_id: number;
}

/** SSE frames on GET /api/workspaces/{id}/chats/{chatId}/events. */
export type WorkspaceChatEvent =
  | { type: "iteration"; n: number; tools?: string[] }
  | { type: "done" }
  | { type: "error"; message: string };

export const WORKSPACE_CHAT_EVENT_NAMES = ["iteration", "done", "error"] as const;

/** Start a deep run; the user message is recorded immediately. */
export function startAsyncWorkspaceChat(
  id: number,
  message: string,
  chatId?: number,
): Promise<WorkspaceChatAsyncAccepted> {
  return request<WorkspaceChatAsyncAccepted>(
    `/api/workspaces/${id}/chat/async`,
    jsonInit("POST", { message, chat_id: chatId }),
  );
}

/** EventSource URL for a deep run's progress, resuming after `afterSeq`. */
export function workspaceChatEventsUrl(
  id: number,
  chatId: number,
  afterSeq: number,
): string {
  return `${ANALYSIS_EVENTS_BASE}/api/workspaces/${id}/chats/${chatId}/events${qs(
    { after: afterSeq },
  )}`;
}

export function cancelAsyncWorkspaceChat(
  id: number,
  chatId: number,
): Promise<{ job_id: number }> {
  return request<{ job_id: number }>(
    `/api/workspaces/${id}/chats/${chatId}/cancel`,
    jsonInit("POST", {}),
  );
}

// --- story mode (grounded narrative over a fact-set, schema v15) -------------

export type StoryStatus = AnalysisStatus;
export type StoryInputType = "story" | "investigation" | "workspace" | "topic";
export type StoryLength = "brief" | "standard" | "feature";
export type StoryTone = "neutral" | "explanatory";

/** SSE frames on GET /api/stories/{id}/events (one structured synth call, so
 *  no per-iteration events — just stage completions + terminal). */
export const STORY_EVENT_NAMES = ["section_completed", "done", "error"] as const;

export interface StorySource {
  ref: string;
  document_id: number | null;
  title: string | null;
  url: string | null;
  source_name: string | null;
  quote: string | null;
  occurred_on: string | null;
  finding_id: number | null;
}

export interface StoryGrounding {
  menu_size: number;
  cited_count: number;
  stripped_markers: string[];
  regenerated: boolean;
  edited: boolean;
}

export interface StoryDetail {
  id: number;
  title: string | null;
  subject: string;
  input_type: StoryInputType;
  status: StoryStatus;
  narrative_md: string | null;
  sources: StorySource[];
  grounding: StoryGrounding | null;
  visibility: Visibility;
  owner_id: number | null;
  last_seq: number;
  error: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface StoryListItem {
  id: number;
  title: string | null;
  subject: string;
  input_type: StoryInputType;
  status: StoryStatus;
  created_at: string;
}

export interface StoryPage {
  items: StoryListItem[];
  total: number;
}

export interface StoryAccepted {
  story_id: number;
  job_id: number;
}

export interface StoryCreateInput {
  story_id?: number;
  investigation_id?: number;
  workspace_id?: number;
  topic?: string;
  since?: string;
  until?: string;
  options?: { length?: StoryLength; tone?: StoryTone; style?: string };
  visibility?: Visibility;
}

export function createStory(input: StoryCreateInput): Promise<StoryAccepted> {
  return request<StoryAccepted>("/api/stories", jsonInit("POST", input));
}

export function listStories(
  params: { limit?: number; offset?: number } = {},
): Promise<StoryPage> {
  return request<StoryPage>(`/api/stories${qs(params)}`);
}

export function getStory(id: number): Promise<StoryDetail> {
  return request<StoryDetail>(`/api/stories/${id}`);
}

export function storyEventsUrl(id: number, afterSeq: number): string {
  return `${ANALYSIS_EVENTS_BASE}/api/stories/${id}/events${qs({ after: afterSeq })}`;
}

export function cancelStory(id: number): Promise<{ job_id: number }> {
  return request<{ job_id: number }>(
    `/api/stories/${id}/cancel`,
    jsonInit("POST", {}),
  );
}

/** Hand-edit a story's generated narrative. */
export function editStory(
  id: number,
  payload: { title?: string; narrative_md?: string },
): Promise<StoryDetail> {
  return request<StoryDetail>(`/api/stories/${id}`, jsonInit("PATCH", payload));
}

/** Cross-question a story — grounded answer from its facts. */
export function askStory(id: number, question: string): Promise<DocumentAnswer> {
  return request<DocumentAnswer>(
    `/api/stories/${id}/ask`,
    jsonInit("POST", { question }),
  );
}

/** Cross-question a finding — grounded answer from it + its cited source. */
export function askPost(id: number, question: string): Promise<DocumentAnswer> {
  return request<DocumentAnswer>(
    `/api/posts/${id}/ask`,
    jsonInit("POST", { question }),
  );
}

export function getGlobalPostSettings(): Promise<PostSettings> {
  return request<PostSettings>("/api/social/settings");
}

export function updateGlobalPostSettings(
  patch: Partial<PostSettings>,
): Promise<PostSettings> {
  return request<PostSettings>("/api/social/settings", jsonInit("PUT", patch));
}

export function listWorkspaces(): Promise<Workspace[]> {
  return request<Workspace[]>("/api/workspaces");
}

export function getWorkspace(id: number): Promise<Workspace> {
  return request<Workspace>(`/api/workspaces/${id}`);
}

export function createWorkspace(payload: WorkspaceCreate): Promise<Workspace> {
  return request<Workspace>("/api/workspaces", jsonInit("POST", payload));
}

export function updateWorkspace(
  id: number,
  payload: WorkspaceUpdate,
): Promise<Workspace> {
  return request<Workspace>(`/api/workspaces/${id}`, jsonInit("PATCH", payload));
}

export interface WorkspaceSourceOption {
  id: number;
  name: string;
  doc_count: number;
}

export interface WorkspaceSourceSuggestions {
  current: WorkspaceSourceOption[];
  suggestions: WorkspaceSourceOption[];
}

export function getWorkspaceSourceSuggestions(
  id: number,
): Promise<WorkspaceSourceSuggestions> {
  return request<WorkspaceSourceSuggestions>(
    `/api/workspaces/${id}/source-suggestions`,
  );
}

export function deleteWorkspace(id: number): Promise<void> {
  return request<void>(`/api/workspaces/${id}`, { method: "DELETE" });
}

export function getWorkspaceFeed(
  id: number,
  page = 1,
  pageSize = 20,
): Promise<Page<DocumentListItem>> {
  return request<Page<DocumentListItem>>(
    `/api/workspaces/${id}/feed${qs({ page, page_size: pageSize })}`,
  );
}

// --- workspace agent (chat) -------------------------------------------------

export interface ChatTurn {
  role: string;
  text: string;
  tools_used: string[];
  finding_id: number | null;
  finding_title: string | null;
}

export interface WorkspaceChatResponse {
  chat_id: number;
  reply: string;
  transcript: ChatTurn[];
  tools_used: string[];
  finding: Post | null;
  turns_completed: number;
  spent_usd: number;
  budget_remaining_usd: number;
}

export interface WorkspaceChatSummary {
  id: number;
  title: string;
  created_at: string;
  updated_at: string | null;
}

export interface WorkspaceChatDetail {
  id: number;
  title: string;
  transcript: ChatTurn[];
  created_at: string;
  updated_at: string | null;
}

/** Send one message; omit chatId to start a new conversation. */
export function workspaceChat(
  id: number,
  message: string,
  chatId?: number,
): Promise<WorkspaceChatResponse> {
  return request<WorkspaceChatResponse>(
    `/api/workspaces/${id}/chat`,
    jsonInit("POST", { message, chat_id: chatId }),
  );
}

export function listWorkspaceChats(id: number): Promise<WorkspaceChatSummary[]> {
  return request<WorkspaceChatSummary[]>(`/api/workspaces/${id}/chats`);
}

export function getWorkspaceChat(
  id: number,
  chatId: number,
): Promise<WorkspaceChatDetail> {
  return request<WorkspaceChatDetail>(`/api/workspaces/${id}/chats/${chatId}`);
}

export function deleteWorkspaceChat(id: number, chatId: number): Promise<void> {
  return request<void>(`/api/workspaces/${id}/chats/${chatId}`, {
    method: "DELETE",
  });
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
  // "all" is the unfiltered default — send nothing (see DossierScope).
  const scope = params.scope === "all" ? undefined : params.scope;
  return request<Page<AnalysisListItem>>(
    `/api/analyses${qs({ ...params, scope })}`,
  );
}

export function getAnalysis(id: number): Promise<AnalysisDetail> {
  return request<AnalysisDetail>(`/api/analyses/${id}`);
}

/** 202 — cancellation is asynchronous; the snapshot flips when it lands. */
export function cancelAnalysis(id: number): Promise<void> {
  return request<void>(`/api/analyses/${id}/cancel`, { method: "POST" });
}

/**
 * Owner-or-admin visibility PATCH (body mirrors backend
 * DossierVisibilityUpdate). Two-step share protocol (design §1 cascade):
 * the unconfirmed call (`confirmDocuments=false`) succeeds only when no
 * private documents are cited; otherwise it answers 409 carrying the
 * confirmation list (see shareConfirmationDocuments), and the caller
 * re-PATCHes with `confirmDocuments=true` to run the cascade. A
 * shared→private downgrade is always 409 (writeback already compounded).
 */
export function setAnalysisVisibility(
  id: number,
  visibility: Visibility,
  confirmDocuments = false,
): Promise<DossierVisibilityResult> {
  return request<DossierVisibilityResult>(
    `/api/analyses/${id}`,
    jsonInit("PATCH", { visibility, confirm_documents: confirmDocuments }),
  );
}

/**
 * Extracts the share-cascade confirmation list from a 409's structured
 * detail. Shape-tolerant — accepts `{detail: {documents: [...]}}`,
 * `{detail: [...]}`, or `{documents: [...]}` — and returns null for any
 * other 409 (downgrade refusal, foreign private evidence), whose string
 * detail renders as an error instead.
 */
export function shareConfirmationDocuments(
  error: unknown,
): ShareDocumentRef[] | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const body = error.body as { detail?: unknown; documents?: unknown } | null;
  const detail = body?.detail as
    | { documents?: unknown }
    | unknown[]
    | null
    | undefined;
  const candidates: unknown[] = [
    Array.isArray(detail) ? detail : (detail as { documents?: unknown })?.documents,
    body?.documents,
  ];
  for (const candidate of candidates) {
    if (!Array.isArray(candidate) || candidate.length === 0) continue;
    const docs: ShareDocumentRef[] = [];
    for (const item of candidate) {
      if (typeof item !== "object" || item === null) return null;
      const record = item as { id?: unknown; title?: unknown };
      if (typeof record.id !== "number") return null;
      docs.push({
        id: record.id,
        title: typeof record.title === "string" ? record.title : null,
      });
    }
    return docs;
  }
  return null;
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

// ---------------------------------------------------------------------------
// Investigations (Phase 4)
// ---------------------------------------------------------------------------

/**
 * Defensive section normalizer: the canonical contract is a keyed object, but
 * a dossier_section-row backend may ship `[{section, content}]` instead. Both
 * shapes (and anything else) normalize to InvestigationSections; unknown keys
 * are dropped.
 */
function normalizeInvestigationSections(raw: unknown): InvestigationSections {
  const sections: Record<string, unknown> = {};
  if (Array.isArray(raw)) {
    for (const row of raw) {
      if (typeof row !== "object" || row === null) continue;
      const record = row as Record<string, unknown>;
      const key = record.section ?? record.stage ?? record.key;
      const content = record.content ?? record.payload;
      if (typeof key === "string") sections[key] = content;
    }
  } else if (typeof raw === "object" && raw !== null) {
    Object.assign(sections, raw as Record<string, unknown>);
  }
  const out: InvestigationSections = {};
  for (const key of INVESTIGATION_SECTION_KEYS) {
    const value = sections[key];
    if (value !== undefined && value !== null && typeof value === "object") {
      out[key] = value as never;
    }
  }
  return out;
}

/** 202 — the investigation runs as a background job; follow it via SSE. */
export function createInvestigation(
  payload: InvestigationCreate,
): Promise<InvestigationAccepted> {
  return request<InvestigationAccepted>(
    "/api/investigations",
    jsonInit("POST", payload),
  );
}

export function listInvestigations(
  params: InvestigationListParams = {},
): Promise<Page<InvestigationListItem>> {
  // "all" is the unfiltered default — send nothing (see DossierScope).
  const scope = params.scope === "all" ? undefined : params.scope;
  return request<Page<InvestigationListItem>>(
    `/api/investigations${qs({ ...params, scope })}`,
  );
}

/** Snapshot, normalized so consumers always see canonical arrays/sections. */
export async function getInvestigation(id: number): Promise<InvestigationDetail> {
  const raw = await request<InvestigationDetail>(`/api/investigations/${id}`);
  return {
    ...raw,
    stages: raw.stages ?? [],
    sections: normalizeInvestigationSections(raw.sections),
    questions: raw.questions ?? [],
    findings: (raw.findings ?? []).map((finding) => ({
      ...finding,
      evidence: finding.evidence ?? [],
    })),
    last_seq: raw.last_seq ?? 0,
  };
}

/** 202 — cancellation is asynchronous; the snapshot flips when it lands. */
export function cancelInvestigation(id: number): Promise<void> {
  return request<void>(`/api/investigations/${id}/cancel`, { method: "POST" });
}

/** Owner-or-admin visibility PATCH — see setAnalysisVisibility (two-step
 *  share protocol + 409 rules). */
export function setInvestigationVisibility(
  id: number,
  visibility: Visibility,
  confirmDocuments = false,
): Promise<DossierVisibilityResult> {
  return request<DossierVisibilityResult>(
    `/api/investigations/${id}`,
    jsonInit("PATCH", { visibility, confirm_documents: confirmDocuments }),
  );
}

/**
 * 202 — manual recursion: spawns a child investigation seeded from an open
 * question (sets question.spawned_dossier_id + child.parent_question_id).
 */
export function investigateQuestion(
  questionId: number,
): Promise<InvestigationAccepted> {
  return request<InvestigationAccepted>(
    `/api/questions/${questionId}/investigate`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Admin (v0.2 Phase D) — everything behind require_admin (403 for members)
// ---------------------------------------------------------------------------

function decodeAdminUser(raw: unknown): AdminUser | null {
  const record = asRecord(raw);
  if (!record) return null;
  const id = asFiniteNumber(record.id);
  if (id === null || typeof record.email !== "string") return null;
  return {
    id,
    email: record.email,
    name: typeof record.name === "string" ? record.name : null,
    avatar_url: typeof record.avatar_url === "string" ? record.avatar_url : null,
    role: record.role === "admin" ? "admin" : "member",
    disabled: record.disabled === true,
    daily_budget_usd: asFiniteNumber(record.daily_budget_usd),
    investigation_daily_budget_usd: asFiniteNumber(
      record.investigation_daily_budget_usd,
    ),
    created_at: typeof record.created_at === "string" ? record.created_at : null,
    last_login_at:
      typeof record.last_login_at === "string" ? record.last_login_at : null,
  };
}

export async function listAdminUsers(): Promise<AdminUser[]> {
  const raw = await request<unknown>("/api/admin/users");
  const rows = Array.isArray(raw)
    ? raw
    : (asRecord(raw)?.items ?? asRecord(raw)?.users);
  if (!Array.isArray(rows)) return [];
  return rows
    .map(decodeAdminUser)
    .filter((user): user is AdminUser => user !== null);
}

/** Role / disable / budget-override management. Explicit JSON nulls clear a
 *  budget override back to the member default; absent fields are untouched. */
export async function updateAdminUser(
  id: number,
  payload: AdminUserUpdate,
): Promise<AdminUser | null> {
  const raw = await request<unknown>(
    `/api/admin/users/${id}`,
    jsonInit("PATCH", payload),
  );
  return decodeAdminUser(raw);
}

// Invites are the one pinned admin contract (Phase B shipped them).

export function listInvites(): Promise<Invite[]> {
  return request<Invite[]>("/api/admin/invites");
}

/** 201; 409 when the email is already invited. */
export function createInvite(payload: InviteCreate): Promise<Invite> {
  return request<Invite>("/api/admin/invites", jsonInit("POST", payload));
}

/** 204; 404 when the invite does not exist. */
export function deleteInvite(email: string): Promise<void> {
  return request<void>(`/api/admin/invites/${encodeURIComponent(email)}`, {
    method: "DELETE",
  });
}

function decodeAdminSettings(raw: unknown): AdminSettings {
  let record = asRecord(raw) ?? {};
  const nested = asRecord(record.settings);
  if (nested) record = nested;
  if (Array.isArray(raw)) {
    // Tolerate the literal app_setting rows: [{key, value}, ...].
    const map: Record<string, unknown> = {};
    for (const item of raw) {
      const row = asRecord(item);
      if (row && typeof row.key === "string") map[row.key] = row.value;
    }
    record = map;
  }
  return {
    global_daily_budget_usd: pickNumber(record, ["global_daily_budget_usd"]),
    daily_llm_budget_usd: pickNumber(record, ["daily_llm_budget_usd"]),
    investigation_daily_budget_usd: pickNumber(record, [
      "investigation_daily_budget_usd",
    ]),
    member_daily_budget_usd: pickNumber(record, ["member_daily_budget_usd"]),
    member_investigation_daily_budget_usd: pickNumber(record, [
      "member_investigation_daily_budget_usd",
    ]),
  };
}

/** The three seeded app_setting budgets (design §4). */
export async function getAdminSettings(): Promise<AdminSettings> {
  return decodeAdminSettings(await request<unknown>("/api/admin/settings"));
}

export async function updateAdminSettings(
  payload: AdminSettingsUpdate,
): Promise<AdminSettings> {
  return decodeAdminSettings(
    await request<unknown>("/api/admin/settings", jsonInit("PATCH", payload)),
  );
}

function decodeAdminSpendUser(raw: unknown): AdminSpendUser | null {
  const record = asRecord(raw);
  if (!record) return null;
  return {
    user_id: asFiniteNumber(record.user_id ?? record.id),
    email: typeof record.email === "string" ? record.email : null,
    name: typeof record.name === "string" ? record.name : null,
    today_usd:
      pickNumber(record, [
        "today_usd",
        "today_spent_usd",
        "spent_today_usd",
        "cost_usd",
        "total_usd",
      ]) ?? 0,
    cap_usd: pickNumber(record, [
      "cap_usd",
      "daily_cap_usd",
      "daily_budget_usd",
      "effective_daily_budget_usd",
    ]),
    investigation_cap_usd: pickNumber(record, [
      "investigation_cap_usd",
      "investigation_daily_budget_usd",
    ]),
    days: asSpendDays(record.days),
  };
}

/** System totals + per-user breakdown (design §4 admin spend split). */
export async function getAdminSpend(days = 7): Promise<AdminSpend> {
  const raw = await request<unknown>(`/api/admin/spend${qs({ days })}`);
  const record = asRecord(raw) ?? {};
  const usersRaw =
    [record.users, record.per_user, record.by_user, record.members].find(
      (candidate): candidate is unknown[] => Array.isArray(candidate),
    ) ?? [];
  return {
    global_cap_usd: pickNumber(record, [
      "global_cap_usd",
      "global_daily_budget_usd",
      "daily_cap_usd",
      "cap_usd",
    ]),
    global_today_usd:
      pickNumber(record, [
        "global_today_usd",
        "today_total_usd",
        "total_today_usd",
        "today_spent_usd",
        "today_usd",
      ]) ?? 0,
    days: asSpendDays(record.days),
    users: usersRaw
      .map(decodeAdminSpendUser)
      .filter((user): user is AdminSpendUser => user !== null),
  };
}

// --- content pipeline (campaigns -> review queue -> publish) ----------------

export type ContentFormat =
  | "ig_card"
  | "ig_carousel"
  | "x_thread"
  | "linkedin_post"
  | "ig_reel"
  | "meme";

/** Content payload for the "meme" format (platform "instagram"). Rendered
 * server-side to one JPEG whose sha rides in card_shas (previewed via
 * cardUrl, like ig_card); edits go through POST /content/{id}/rerender. */
export interface MemeContent {
  image_query: string;
  top_text: string;
  bottom_text: string;
  caption: string;
  hashtags: string[];
  source_label?: string;
  alt_text?: string;
}

export type ContentItemStatus =
  | "draft"
  | "verifying"
  | "approved"
  | "scheduled"
  | "published"
  | "rejected"
  | "failed"
  | "flagged"
  | "corrected"
  | "retracted";

/** Grounding telemetry for a generated item (how many cited menu items, etc.). */
export interface ContentGrounding {
  menu_size?: number;
  cited_count?: number;
  stripped_markers?: string[];
  regenerated?: boolean;
  edited?: boolean;
}

export interface ContentItem {
  id: number;
  campaign_id: number;
  platform: string;
  format: ContentFormat;
  status: ContentItemStatus;
  content: Record<string, unknown>;
  sources: StorySource[];
  grounding: ContentGrounding;
  gate?: GateReport;
  card_shas: string[];
  scheduled_at: string | null;
  published_at: string | null;
  publish_ref: Record<string, unknown> | null;
  error: string | null;
  edited: boolean;
  created_at: string;
}

/** The editorial verification-gate report (S2) carried on a content item. */
export interface GateReport {
  verdict?: "pass" | "flagged" | "error";
  checked?: number;
  flagged?: { statement: string; reason: string; detail?: string }[];
  contested?: { claim_id: number; text: string; verdict: string }[];
  enforcing?: boolean;
  error?: string | null;
}

/** A cited source as the trust panel shows it (S6). */
export interface TrustSource {
  ref: string;
  document_id: number | null;
  source_name: string | null;
  title: string | null;
  url: string | null;
  quote: string | null;
  credibility_tier: number | null;
  reliability_score: number | null;
}

/** The reader-facing "why trust this" report (S6). */
export interface TrustReport {
  gate_verdict: string | null;
  flagged_count: number;
  confidence: number | null;
  sources: TrustSource[];
  tier_mix: Record<string, number>;
  independent_publishers: number;
  balance_score: number;
  balance_label: string;
  one_sided: boolean;
  contested: { claim_id: number; text: string; verdict: string }[];
  corrections: { action: string; kind: string; to_verdict?: string; detail?: string }[];
  freshness: string | null;
  ai_disclosure: boolean;
}

export async function getContentTrust(itemId: number): Promise<TrustReport> {
  return request<TrustReport>(`/api/content/${itemId}/trust`);
}

export interface CampaignListItem {
  id: number;
  subject: string;
  input_type: string;
  status: string;
  formats: string[];
  item_count: number;
  created_at: string;
}

/** The editorial planner's decision on an 'auto' campaign (formats: []). */
export interface EditorialPlan {
  significance: number; // 1-5
  significance_label: string; // breaking | major | notable | minor | routine
  big_news: boolean;
  angle: string;
  rationale: string;
  picks: {
    format: ContentFormat;
    reason: string;
    media: "stock_photo" | "stock_video" | "none";
    media_query: string;
  }[];
}

export interface CampaignDetail {
  id: number;
  subject: string;
  input_type: string;
  status: string;
  formats: string[];
  plan: EditorialPlan | null;
  error: string | null;
  items: ContentItem[];
  created_at: string;
}

export interface CampaignAccepted {
  campaign_id: number;
  job_id: number;
}

export interface CampaignCreate {
  topic?: string;
  story_dossier_id?: number;
  investigation_id?: number;
  workspace_id?: number;
  story_id?: number;
  /** Empty => 'auto': the editorial planner picks the formats and media. */
  formats: ContentFormat[];
  options?: ReelOptions;
}

/** All content formats, in display order (mirrors backend CONTENT_FORMATS). */
export const CONTENT_FORMATS: { value: ContentFormat; label: string }[] = [
  { value: "ig_reel", label: "Instagram Reel" },
  { value: "ig_card", label: "Instagram Card" },
  { value: "ig_carousel", label: "Instagram Carousel" },
  { value: "meme", label: "Meme" },
  { value: "x_thread", label: "X Thread" },
  { value: "linkedin_post", label: "LinkedIn Post" },
];

export const reelUrl = (sha: string) => `/api/social/reel/${sha}.mp4`;
export const cardUrl = (sha: string) => `/api/social/card/${sha}.jpg`;

export function createCampaign(body: CampaignCreate): Promise<CampaignAccepted> {
  return request<CampaignAccepted>("/api/campaigns", jsonInit("POST", body));
}

export function listCampaigns(
  workspaceId?: number,
): Promise<{ items: CampaignListItem[]; total: number }> {
  const query = workspaceId ? qs({ workspace_id: workspaceId }) : "";
  return request<{ items: CampaignListItem[]; total: number }>(`/api/campaigns${query}`);
}

export function getCampaign(id: number): Promise<CampaignDetail> {
  return request<CampaignDetail>(`/api/campaigns/${id}`);
}

export function approveContent(id: number): Promise<ContentItem> {
  return request<ContentItem>(`/api/content/${id}/approve`, jsonInit("POST", {}));
}

export function rejectContent(id: number): Promise<ContentItem> {
  return request<ContentItem>(`/api/content/${id}/reject`, jsonInit("POST", {}));
}

export function scheduleContent(id: number, scheduled_at: string): Promise<ContentItem> {
  return request<ContentItem>(
    `/api/content/${id}/schedule`,
    jsonInit("POST", { scheduled_at }),
  );
}

export function publishContent(
  id: number,
  target: "direct" | "zapier" = "direct",
): Promise<JobAccepted> {
  const query = target === "zapier" ? qs({ target }) : "";
  return request<JobAccepted>(`/api/content/${id}/publish${query}`, jsonInit("POST", {}));
}

/** Hand-edit a draft's payload (any format). Replaces the whole content object;
 * the server flags it edited. For image/video formats, follow with a re-render
 * so the rendered media reflects the edit. */
export function editContent(
  id: number,
  content: Record<string, unknown>,
): Promise<ContentItem> {
  return request<ContentItem>(`/api/content/${id}`, jsonInit("PATCH", { content }));
}

export function cancelCampaign(id: number): Promise<JobAccepted> {
  return request<JobAccepted>(`/api/campaigns/${id}/cancel`, jsonInit("POST", {}));
}

export function deleteCampaign(id: number): Promise<void> {
  return request<void>(`/api/campaigns/${id}`, { method: "DELETE" });
}

// --- reel controls: voices, render options, re-render -----------------------

export interface VoiceOption {
  id: string;
  name: string;
  description: string;
}

/** On-screen caption styles for reels (mirrors backend reel_caption_style). */
export type CaptionStyle = "karaoke" | "lower_third" | "centered" | "boxed";
export const CAPTION_STYLES: { value: CaptionStyle; label: string }[] = [
  { value: "karaoke", label: "Karaoke (word-by-word)" },
  { value: "lower_third", label: "Lower third" },
  { value: "centered", label: "Centered" },
  { value: "boxed", label: "Boxed / TikTok" },
];

/** Scene media treatment for reels (mirrors backend reel_visual_style):
 * 'fitted' keeps the whole photo/clip on a solid theme-colour canvas (never
 * side-cropped); 'poster' is the viral news-page look (full-bleed media, bold
 * bottom accent caption); 'cover' is the legacy full-bleed crop under a
 * scrim. */
export type VisualStyle = "fitted" | "poster" | "cover";
export const VISUAL_STYLES: { value: VisualStyle; label: string }[] = [
  { value: "fitted", label: "Fitted (solid background)" },
  { value: "poster", label: "Poster (bottom caption)" },
  { value: "cover", label: "Full-bleed (cropped)" },
];

/** Generation + render controls for a campaign (mirrors backend ContentOptions).
 * Also reused for reel re-render. */
export interface ContentOptions {
  // shared voice/shape knobs
  tone?: string;
  style?: string | null;
  hashtag_count?: number;
  slide_count?: number; // carousel target
  thread_length?: number; // x-thread target
  scene_count?: number; // reel target
  // reel render controls
  voice_id?: string | null;
  tts_engine?: string | null;
  music?: boolean;
  music_volume?: number | null;
  presenter?: boolean;
  caption_style?: CaptionStyle | null;
  visual_style?: VisualStyle | null;
  video?: boolean | null;
  character_id?: number | null;
  script_type?: string | null;
}

/** @deprecated alias kept for older imports — use ContentOptions. */
export type ReelOptions = ContentOptions;

export function getVoices(): Promise<VoiceOption[]> {
  return request<VoiceOption[]>("/api/voices");
}

/** Audition a voice — returns an object URL for the synthesized clip (caller
 * must URL.revokeObjectURL when done). */
export async function auditionVoice(body: { voice_id: string; text?: string }): Promise<string> {
  const res = await fetch("/api/voices/audition", jsonInit("POST", body));
  if (!res.ok) await raise(res);
  return URL.createObjectURL(await res.blob());
}

/** A voicebox voice profile (the 'vb:' voices). */
export interface VoiceboxProfile {
  id: string;
  name: string;
  language?: string | null;
  description?: string | null;
  preset_voice_id?: string | null;
  default_engine?: string | null;
}

/** A voicebox PRESET (a Kokoro catalog voice, addable as a profile). */
export interface VoiceboxPreset {
  voice_id: string;
  name?: string | null;
  gender?: string | null;
  language?: string | null;
}

export function getVoiceboxProfiles(): Promise<VoiceboxProfile[]> {
  return request<VoiceboxProfile[]>("/api/voices/profiles");
}

export async function getVoiceboxCatalog(engine = "kokoro"): Promise<VoiceboxPreset[]> {
  const data = await request<{ voices?: VoiceboxPreset[] }>(`/api/voices/presets/${engine}`);
  return data.voices ?? [];
}

export function addVoiceboxProfile(body: Record<string, unknown>): Promise<VoiceboxProfile> {
  return request<VoiceboxProfile>("/api/voices/profiles", jsonInit("POST", body));
}

export function deleteVoiceboxProfile(id: string): Promise<void> {
  return request<void>(`/api/voices/profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// --- characters (the shared persona roster) ---------------------------------

export interface Character {
  id: number;
  name: string;
  description: string;
  speaking_style: string;
  sample_line: string;
  voice_id: string | null;
  heygen_avatar_id: string | null;
  catchphrases: string[];
  sign_off: string;
  avatar_sha: string | null;
  owner_id: number | null;
  created_at: string;
  updated_at: string | null;
}

export interface CharacterInput {
  name: string;
  description?: string;
  speaking_style?: string;
  sample_line?: string;
  voice_id?: string | null;
  heygen_avatar_id?: string | null;
  catchphrases?: string[];
  sign_off?: string;
  avatar_sha?: string | null;
}

export function listCharacters(): Promise<Character[]> {
  return request<Character[]>("/api/characters");
}

export function createCharacter(body: CharacterInput): Promise<Character> {
  return request<Character>("/api/characters", jsonInit("POST", body));
}

export function updateCharacter(id: number, body: Partial<CharacterInput>): Promise<Character> {
  return request<Character>(`/api/characters/${id}`, jsonInit("PATCH", body));
}

export function deleteCharacter(id: number): Promise<void> {
  return request<void>(`/api/characters/${id}`, { method: "DELETE" });
}

// --- script-type presets -----------------------------------------------------

export interface ScriptPreset {
  id: string; // builtin slug or 'custom:<n>'
  name: string;
  guidance: string;
  formats: string[];
  scene_count: number | null;
  caption_style: string | null;
  visual_style: string | null;
  builtin: boolean;
}

export interface ScriptPresetInput {
  name: string;
  guidance?: string;
  formats?: string[];
  scene_count?: number | null;
  caption_style?: string | null;
  visual_style?: string | null;
}

export function listScriptPresets(): Promise<ScriptPreset[]> {
  return request<ScriptPreset[]>("/api/script-presets");
}

export function createScriptPreset(body: ScriptPresetInput): Promise<ScriptPreset> {
  return request<ScriptPreset>("/api/script-presets", jsonInit("POST", body));
}

export function updateScriptPreset(id: string, body: Partial<ScriptPresetInput>): Promise<ScriptPreset> {
  return request<ScriptPreset>(`/api/script-presets/${encodeURIComponent(id)}`, jsonInit("PATCH", body));
}

export function deleteScriptPreset(id: string): Promise<void> {
  return request<void>(`/api/script-presets/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// --- workspace channel + global channel settings ----------------------------

export type ChannelPlatform = "youtube" | "instagram" | "x" | "linkedin" | "other";

export interface WorkspaceChannel {
  workspace_id: number;
  platform: ChannelPlatform;
  channel_name: string;
  channel_handle: string;
  external_id: string | null;
  zapier_webhook_url: string | null;
  webhook_set: boolean;
  default_voice_id: string | null;
  default_character_id: number | null;
  default_script_type: string | null;
  has_credentials: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface WorkspaceChannelUpdate {
  platform?: ChannelPlatform;
  channel_name?: string;
  channel_handle?: string;
  external_id?: string | null;
  zapier_webhook_url?: string | null;
  default_voice_id?: string | null;
  default_character_id?: number | null;
  default_script_type?: string | null;
}

export function getWorkspaceChannel(id: number): Promise<WorkspaceChannel> {
  return request<WorkspaceChannel>(`/api/workspaces/${id}/channel`);
}

export function updateWorkspaceChannel(id: number, body: WorkspaceChannelUpdate): Promise<WorkspaceChannel> {
  return request<WorkspaceChannel>(`/api/workspaces/${id}/channel`, jsonInit("PUT", body));
}

export interface ChannelSettings {
  default_voice_id: string | null;
  default_character_id: number | null;
  default_script_type: string | null;
}

export function getChannelSettings(): Promise<ChannelSettings> {
  return request<ChannelSettings>("/api/social/channel-settings");
}

export function updateChannelSettings(body: Partial<ChannelSettings>): Promise<ChannelSettings> {
  return request<ChannelSettings>("/api/social/channel-settings", jsonInit("PUT", body));
}

/** Optional reel features configured on the server (e.g. the HeyGen presenter). */
export interface ReelCapabilities {
  presenter: boolean;
  video: boolean;
  /** Whether a Zapier webhook is configured ("Send to Zapier" publish route). */
  zapier: boolean;
}

export function getReelCapabilities(): Promise<ReelCapabilities> {
  return request<ReelCapabilities>("/api/content/capabilities");
}

export function rerenderContent(
  id: number,
  body: { content?: Record<string, unknown>; options?: ReelOptions },
): Promise<JobAccepted> {
  return request<JobAccepted>(
    `/api/content/${id}/rerender`,
    jsonInit("POST", { content: body.content ?? null, options: body.options ?? {} }),
  );
}

// --- the reel factory --------------------------------------------------------

export interface FactoryRunRequest {
  count?: number;
  window_hours?: number;
  options?: ContentOptions;
  workspace_id?: number;
}

/** One commissioned subject inside a factory run. */
export interface FactoryAssignment {
  subject: string;
  kind: string; // thread | event | topic
  heat: number;
  angle?: string;
  reason?: string;
}

export interface FactoryCampaignRef {
  campaign_id: number;
  job_id: number;
  subject: string;
}

/** A past/running factory run: the job row + what its events reported. */
export interface FactoryRun {
  job_id: number;
  status: string; // queued | running | done | failed | cancelled
  created_at: string;
  finished_at: string | null;
  error: string | null;
  result: string | null;
  candidates: number;
  assignments: FactoryAssignment[];
  campaigns: FactoryCampaignRef[];
}

/** Start a factory run: scout the feed's hottest subjects and commission one
 * reel-led campaign per pick. */
export function startFactoryRun(body: FactoryRunRequest = {}): Promise<JobAccepted> {
  return request<JobAccepted>("/api/factory/reels", jsonInit("POST", body));
}

export function listFactoryRuns(): Promise<{ items: FactoryRun[] }> {
  return request<{ items: FactoryRun[] }>("/api/factory/runs");
}

/** Ask the worker to stop a queued/running factory run. Campaigns it already
 * commissioned keep generating — cancel those individually. */
export function cancelFactoryRun(jobId: number): Promise<JobAccepted> {
  return request<JobAccepted>(`/api/factory/runs/${jobId}/cancel`, jsonInit("POST", {}));
}
