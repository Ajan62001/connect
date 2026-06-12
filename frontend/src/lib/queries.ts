"use client";

/**
 * TanStack Query hooks over the typed API client. Components only ever talk
 * to these hooks; mutations invalidate the query keys they affect.
 */

import { useEffect, useRef, useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import * as api from "@/lib/api";
import { ANALYSIS_EVENT_NAMES, INVESTIGATION_EVENT_NAMES } from "@/lib/api";
import type {
  AdminSettingsUpdate,
  AdminUserUpdate,
  AnalysisCreate,
  AnalysisDetail,
  AnalysisEvent,
  AnalysisListParams,
  AnalysisStageName,
  Brief,
  BriefSectionKey,
  ContradictionListParams,
  CursorCreate,
  DocumentAnswer,
  DocumentListParams,
  EntityListParams,
  FeedParams,
  IngestResult,
  IngestText,
  InvestigationCreate,
  InvestigationDetail,
  InvestigationEvent,
  InvestigationListParams,
  InviteCreate,
  PostCreate,
  PostUpdate,
  SearchKind,
  SourceCreate,
  SourceTestRequest,
  SourceUpdate,
  SweepRequest,
  WatchCreate,
  WatchUpdate,
} from "@/lib/api";

export const queryKeys = {
  health: ["health"] as const,
  me: ["me"] as const,
  authMethods: ["auth", "methods"] as const,
  sources: ["sources"] as const,
  documents: ["documents"] as const,
  documentList: (params: DocumentListParams) =>
    ["documents", "list", params] as const,
  document: (id: number) => ["documents", "detail", id] as const,
  feed: (params: FeedParams) => ["feed", params] as const,
  watches: ["watches"] as const,
  watchBadges: ["watches", "badges"] as const,
  posts: ["posts"] as const,
  postList: (documentId?: number) =>
    ["posts", "list", documentId ?? null] as const,
  entities: ["entities"] as const,
  entityList: (params: EntityListParams) => ["entities", "list", params] as const,
  entity: (id: number) => ["entities", "detail", id] as const,
  entityDocuments: (id: number, params: { page?: number; page_size?: number }) =>
    ["entities", "detail", id, "documents", params] as const,
  /** Prefix of every topicViews key, so one invalidation refreshes both. */
  entityViews: (id: number) => ["entities", "detail", id, "views"] as const,
  topicViews: (id: number, topic: string) =>
    ["entities", "detail", id, "views", topic] as const,
  search: (q: string, kind: SearchKind) => ["search", kind, q] as const,
  spend: (days: number) => ["spend", days] as const,
  briefs: ["brief"] as const,
  /** `date` is 'today' or 'YYYY-MM-DD'. */
  brief: (date: string) => ["brief", date] as const,
  event: (id: number) => ["events", "detail", id] as const,
  thread: (id: number) => ["threads", "detail", id] as const,
  calendar: (days: number) => ["calendar", days] as const,
  analyses: ["analyses"] as const,
  analysisList: (params: AnalysisListParams) =>
    ["analyses", "list", params] as const,
  analysis: (id: number) => ["analyses", "detail", id] as const,
  contradictions: ["contradictions"] as const,
  contradictionList: (params: ContradictionListParams) =>
    ["contradictions", "list", params] as const,
  contradiction: (id: number) => ["contradictions", "detail", id] as const,
  contradictionOpenCount: ["contradictions", "open-count"] as const,
  investigations: ["investigations"] as const,
  investigationList: (params: InvestigationListParams) =>
    ["investigations", "list", params] as const,
  investigation: (id: number) => ["investigations", "detail", id] as const,
  admin: ["admin"] as const,
  adminUsers: ["admin", "users"] as const,
  adminInvites: ["admin", "invites"] as const,
  adminSettings: ["admin", "settings"] as const,
  adminSpend: (days: number) => ["admin", "spend", days] as const,
};

// --------------------------------------------------------------------------
// Health
// --------------------------------------------------------------------------

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: api.getHealth,
    refetchInterval: 30_000,
    retry: false,
  });
}

// --------------------------------------------------------------------------
// Auth (v0.2 Phase B)
// --------------------------------------------------------------------------

/**
 * The signed-in user. `retry: false` because the interesting failure is a
 * 401, and the fetch wrapper already redirects to /signin on it.
 */
export function useMe() {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: api.getMe,
    retry: false,
    staleTime: 60_000,
  });
}

/** Which sign-in buttons to render (open endpoint; signin page only). */
export function useAuthMethods() {
  return useQuery({
    queryKey: queryKeys.authMethods,
    queryFn: api.getAuthMethods,
    retry: 1,
    staleTime: Infinity,
  });
}

/** Dev-hatch sign-in; the caller navigates on success. */
export function useDevLogin() {
  return useMutation({ mutationFn: api.devLogin });
}

/**
 * Sign out: server session destroyed, then a HARD navigation to /signin —
 * a full load drops every cached query of the previous user. The explicit
 * clear() is belt-and-braces: nothing of the signed-out user survives even
 * if the navigation is delayed (slow unload, devtools pause, bfcache).
 */
export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      queryClient.clear();
      window.location.assign("/signin");
    },
  });
}

/**
 * Tenancy guard against cross-user cache leaks (design §6): when the
 * signed-in user *changes* within one JS lifetime (every current login path
 * is a hard navigation, but nothing guarantees that forever), drop the whole
 * query cache — watches, briefs, today, spend, dossier lists are all
 * per-user now. Mounted once inside the QueryClientProvider.
 */
export function useUserSwitchCacheReset() {
  const queryClient = useQueryClient();
  const me = useMe();
  const meId = me.data?.id;
  const lastUserIdRef = useRef<number | null>(null);
  useEffect(() => {
    if (meId === undefined) return; // loading / signed out — nothing to compare
    if (lastUserIdRef.current !== null && lastUserIdRef.current !== meId) {
      // A different user is now signed in: every cached query (including the
      // /api/me just observed) belongs to the previous user's view. clear()
      // wipes the cache; active observers refetch as the new user.
      queryClient.clear();
    }
    lastUserIdRef.current = meId;
  }, [meId, queryClient]);
}

// --------------------------------------------------------------------------
// Sources
// --------------------------------------------------------------------------

export function useSources() {
  return useQuery({ queryKey: queryKeys.sources, queryFn: api.listSources });
}

export function useCreateSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: SourceCreate) => api.createSource(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources });
    },
  });
}

export function useUpdateSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: SourceUpdate }) =>
      api.updateSource(id, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources });
    },
  });
}

export function useDeleteSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteSource(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources });
    },
  });
}

export function useTestSource() {
  return useMutation({
    mutationFn: (payload: SourceTestRequest) => api.testSource(payload),
  });
}

export function usePollSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.pollSource(id),
    onSuccess: () => {
      // Poll results land asynchronously; refresh source health when they do.
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources });
    },
  });
}

// --------------------------------------------------------------------------
// Documents / feed / search
// --------------------------------------------------------------------------

export function useDocuments(params: DocumentListParams) {
  return useQuery({
    queryKey: queryKeys.documentList(params),
    queryFn: () => api.listDocuments(params),
    placeholderData: keepPreviousData,
  });
}

export function useDocument(id: number) {
  return useQuery({
    queryKey: queryKeys.document(id),
    queryFn: () => api.getDocument(id),
    enabled: Number.isFinite(id),
  });
}

/** Ask a grounded question about one document — a synchronous single LLM
 *  call, so this is a plain mutation (no cache to invalidate). */
export function useAskDocument(id: number) {
  return useMutation<DocumentAnswer, Error, string>({
    mutationFn: (question: string) => api.askDocument(id, question),
  });
}

export function useFeed(params: FeedParams) {
  return useQuery({
    queryKey: queryKeys.feed(params),
    queryFn: () => api.getFeed(params),
    placeholderData: keepPreviousData,
  });
}

export function useIngest() {
  const queryClient = useQueryClient();
  return useMutation<IngestResult, Error, IngestInput>({
    mutationFn: (input: IngestInput) => {
      switch (input.mode) {
        case "text":
          return api.ingestText(input.payload);
        case "url":
          return api.ingestUrl(input.url);
        case "file":
          return api.ingestFile(input.file);
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources });
    },
  });
}

export type IngestInput =
  | { mode: "text"; payload: IngestText }
  | { mode: "url"; url: string }
  | { mode: "file"; file: File };

/**
 * Manual follow of one extracted link. Invalidates the parent document's
 * detail query (link status flips) and the listings (a followed link may
 * have created a new document).
 */
export function useFetchDocumentLink(documentId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (linkId: number) => api.fetchDocumentLink(linkId),
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.document(documentId),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
    },
  });
}

/**
 * Shares a private document (uploads/pasted text default private, design §1).
 * One-way in practice: once shared, the doc becomes enrichment-eligible and
 * compounds into the shared KB. Refreshes the detail plus every listing the
 * newly-visible document can appear in.
 */
export function useShareDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.setDocumentVisibility(id, "shared"),
    // Invalidate rather than seed from the response: the PATCH may answer
    // with a slimmer row than the GET detail payload.
    onSuccess: (_document, id) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.document(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
    },
  });
}

// --------------------------------------------------------------------------
// Entities (Phase 1)
// --------------------------------------------------------------------------

export function useEntities(
  params: EntityListParams,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: queryKeys.entityList(params),
    queryFn: () => api.listEntities(params),
    placeholderData: keepPreviousData,
    enabled: options.enabled ?? true,
  });
}

export function useEntity(id: number) {
  return useQuery({
    queryKey: queryKeys.entity(id),
    queryFn: () => api.getEntity(id),
    enabled: Number.isFinite(id),
  });
}

export function useEntityDocuments(
  id: number,
  params: { page?: number; page_size?: number },
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.entityDocuments(id, params),
    queryFn: () => api.listEntityDocuments(id, params),
    placeholderData: keepPreviousData,
    enabled: enabled && Number.isFinite(id),
  });
}

// --------------------------------------------------------------------------
// Leader views & position tracking (schema v9)
// --------------------------------------------------------------------------

/** Topic index for an entity's "Views & statements" section. */
export function useEntityViews(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.entityViews(id),
    queryFn: () => api.getEntityViews(id),
    enabled: enabled && Number.isFinite(id),
  });
}

/**
 * Per-topic statement timeline. Reading the endpoint can trigger a governed
 * LLM regeneration of the evolution summary server-side, so the query stays
 * idle until the user explicitly selects a topic (`topic: null` disables it)
 * and a generous staleTime avoids gratuitous re-reads.
 */
export function useTopicViews(entityId: number, topic: string | null) {
  return useQuery({
    queryKey: queryKeys.topicViews(entityId, topic ?? ""),
    queryFn: () => api.getTopicViews(entityId, topic ?? ""),
    enabled:
      Number.isFinite(entityId) && topic !== null && topic.trim().length > 0,
    staleTime: 5 * 60_000,
  });
}

/**
 * Dismisses one position shift. Invalidating the entityViews prefix refreshes
 * both the topic chips (shift counts) and any open per-topic panel.
 */
export function useDismissShift(entityId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (shiftId: number) => api.dismissPositionShift(shiftId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.entityViews(entityId),
      });
    },
  });
}

// --------------------------------------------------------------------------
// Cross-corpus search (documents + entities)
// --------------------------------------------------------------------------

export function useSearch(q: string, kind: SearchKind) {
  return useQuery({
    queryKey: queryKeys.search(q, kind),
    queryFn: () => api.searchCorpus(q, kind),
    placeholderData: keepPreviousData,
    enabled: q.trim().length > 0,
  });
}

// --------------------------------------------------------------------------
// LLM spend / enrichment sweeps (Phase 1)
// --------------------------------------------------------------------------

/**
 * Normalized spend view: my spend (Phase D backend) + the global envelope;
 * `mine` is null against a pre-Phase-D backend and consumers fall back to
 * the global slice (see api.normalizeSpend).
 */
export function useSpend(days = 7) {
  return useQuery({
    queryKey: queryKeys.spend(days),
    queryFn: () => api.getSpendView(days),
    refetchInterval: 60_000,
    retry: false,
  });
}

/**
 * Kick an enrichment sweep. The job runs server-side; refresh everything an
 * enrichment pass can touch (statuses, enrichment payloads, entities, spend).
 */
export function useEnrichmentSweep() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: SweepRequest) => api.sweepEnrichment(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      void queryClient.invalidateQueries({ queryKey: queryKeys.entities });
      void queryClient.invalidateQueries({ queryKey: ["spend"] });
    },
  });
}

// --------------------------------------------------------------------------
// Briefs / events / threads / cursors / calendar (Phase 2)
// --------------------------------------------------------------------------

/**
 * Today's brief — generated lazily server-side on the first fetch of the day,
 * then stable. Shared by the Today page and the sidebar unseen dot; refetches
 * on window focus (overriding the global default) so a brief left open
 * overnight rolls over without a manual reload.
 */
export function useBriefToday() {
  return useQuery({
    queryKey: queryKeys.brief("today"),
    queryFn: api.getBriefToday,
    refetchOnWindowFocus: true,
    staleTime: 60_000,
  });
}

/** One historical day's brief ('YYYY-MM-DD') — immutable once generated. */
export function useBrief(date: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.brief(date),
    queryFn: () => api.getBrief(date),
    staleTime: 5 * 60_000,
    enabled,
  });
}

function withItemSeen(brief: Brief, itemId: number): Brief {
  const sections = { ...brief.sections };
  for (const key of Object.keys(sections) as BriefSectionKey[]) {
    sections[key] = (sections[key] ?? []).map((item) =>
      item.id === itemId ? { ...item, seen: true } : item,
    );
  }
  return { ...brief, sections };
}

/**
 * Marks one brief item seen. Optimistic: flips `seen` in every cached brief
 * immediately, rolling back on error; the server answers 204, so success
 * needs no invalidation.
 */
export function useMarkBriefItemSeen() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (itemId: number) => api.markBriefItemSeen(itemId),
    onMutate: async (itemId) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.briefs });
      const previous = queryClient.getQueriesData<Brief>({
        queryKey: queryKeys.briefs,
      });
      queryClient.setQueriesData<Brief>({ queryKey: queryKeys.briefs }, (old) =>
        old ? withItemSeen(old, itemId) : old,
      );
      return { previous };
    },
    onError: (_error, _itemId, context) => {
      for (const [key, data] of context?.previous ?? []) {
        queryClient.setQueryData(key, data);
      }
    },
  });
}

export function useEvent(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.event(id),
    queryFn: () => api.getEvent(id),
    enabled: enabled && Number.isFinite(id),
  });
}

export function useThread(id: number) {
  return useQuery({
    queryKey: queryKeys.thread(id),
    queryFn: () => api.getThread(id),
    enabled: Number.isFinite(id),
  });
}

export function useCalendar(days = 120) {
  return useQuery({
    queryKey: queryKeys.calendar(days),
    queryFn: () => api.getCalendar(days),
  });
}

/**
 * Upserts a view cursor (fire-and-forget on surface mount). Deliberately does
 * NOT invalidate the surface's detail query: the delta the user is reading
 * was computed against the previous cursor and should survive the visit.
 */
export function usePostCursor() {
  return useMutation({
    mutationFn: (payload: CursorCreate) => api.postCursor(payload),
  });
}

/** Manual T2 promotion of a document into an event (202 + job id). */
export function usePromoteDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.promoteDocument(id),
    onSuccess: (_job, id) => {
      // The job is async; refresh the document so the event chip appears
      // once promotion lands and the user refetches.
      void queryClient.invalidateQueries({ queryKey: queryKeys.document(id) });
    },
  });
}

// --------------------------------------------------------------------------
// Watches
// --------------------------------------------------------------------------

export function useWatches() {
  return useQuery({ queryKey: queryKeys.watches, queryFn: api.listWatches });
}

export function useWatchBadges() {
  return useQuery({
    queryKey: queryKeys.watchBadges,
    queryFn: api.getWatchBadges,
    refetchInterval: 60_000,
  });
}

function useInvalidateWatches() {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.watches });
  };
}

export function useCreateWatch() {
  const invalidate = useInvalidateWatches();
  return useMutation({
    mutationFn: (payload: WatchCreate) => api.createWatch(payload),
    onSuccess: invalidate,
  });
}

export function useUpdateWatch() {
  const invalidate = useInvalidateWatches();
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: WatchUpdate }) =>
      api.updateWatch(id, payload),
    onSuccess: invalidate,
  });
}

export function useDeleteWatch() {
  const invalidate = useInvalidateWatches();
  return useMutation({
    mutationFn: (id: number) => api.deleteWatch(id),
    onSuccess: invalidate,
  });
}

export function useMarkWatchSeen() {
  const invalidate = useInvalidateWatches();
  return useMutation({
    mutationFn: (id: number) => api.markWatchSeen(id),
    onSuccess: invalidate,
  });
}

// --------------------------------------------------------------------------
// Findings board (posts)
// --------------------------------------------------------------------------

export function usePosts(documentId?: number) {
  return useQuery({
    queryKey: queryKeys.postList(documentId),
    queryFn: () => api.listPosts(documentId),
    placeholderData: keepPreviousData,
  });
}

function useInvalidatePosts() {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.posts });
  };
}

export function useCreatePost() {
  const invalidate = useInvalidatePosts();
  return useMutation({
    mutationFn: (payload: PostCreate) => api.createPost(payload),
    onSuccess: invalidate,
  });
}

export function useUpdatePost() {
  const invalidate = useInvalidatePosts();
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: PostUpdate }) =>
      api.updatePost(id, payload),
    onSuccess: invalidate,
  });
}

export function useDeletePost() {
  const invalidate = useInvalidatePosts();
  return useMutation({
    mutationFn: (id: number) => api.deletePost(id),
    onSuccess: invalidate,
  });
}

// --------------------------------------------------------------------------
// Analyses (Phase 3)
// --------------------------------------------------------------------------

export function useAnalyses(params: AnalysisListParams) {
  return useQuery({
    queryKey: queryKeys.analysisList(params),
    queryFn: () => api.listAnalyses(params),
    placeholderData: keepPreviousData,
    // Keep list rows fresh while any visible analysis is still in flight.
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) => item.status === "pending" || item.status === "running",
      )
        ? 5_000
        : false,
  });
}

export function useCreateAnalysis() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: AnalysisCreate) => api.createAnalysis(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.analyses });
    },
  });
}

export function useCancelAnalysis() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.cancelAnalysis(id),
    onSuccess: (_void, id) => {
      // 202 — cancellation is async; refetch so the status flips when it lands.
      void queryClient.invalidateQueries({ queryKey: queryKeys.analysis(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.analyses });
    },
  });
}

/** Both halves of the two-step share PATCH (see api.setAnalysisVisibility):
 *  `confirm: false` probes; `confirm: true` runs the document cascade. */
export interface ShareDossierInput {
  id: number;
  confirm: boolean;
}

/**
 * Flips a private analysis to shared (the confirmed call auto-shares cited
 * private documents server-side, design §1) — so the document listings
 * refresh too.
 */
export function useShareAnalysis() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, confirm }: ShareDossierInput) =>
      api.setAnalysisVisibility(id, "shared", confirm),
    onSuccess: (_result, { id }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.analysis(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.analyses });
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
    },
  });
}

/** One `stage_progress` line, kept out of the query cache (ActivityLog only). */
export interface AnalysisActivityLine {
  seq: number;
  stage: AnalysisStageName;
  message: string;
}

/** stage_progress ring buffer size — old lines fall off the front. */
const ACTIVITY_BUFFER_SIZE = 200;

/** Stable empty buffer returned while activity state belongs to another id. */
const NO_ACTIVITY: AnalysisActivityLine[] = [];

/**
 * Pure reducer applying one SSE event to the cached snapshot. Timestamps set
 * here are client-side approximations; the next snapshot refetch corrects
 * them with server values.
 */
function applyAnalysisEvent(
  detail: AnalysisDetail,
  event: AnalysisEvent,
): AnalysisDetail {
  const now = new Date().toISOString();
  switch (event.type) {
    case "stage_started": {
      const known = detail.stages.some((s) => s.stage === event.stage);
      const stages = known
        ? detail.stages.map((s) =>
            s.stage === event.stage
              ? { ...s, status: "running" as const, started_at: s.started_at ?? now }
              : s,
          )
        : [
            ...detail.stages,
            {
              stage: event.stage,
              status: "running" as const,
              summary: null,
              started_at: now,
              finished_at: null,
            },
          ];
      return { ...detail, status: "running", stages };
    }
    case "stage_completed":
      return {
        ...detail,
        stages: detail.stages.map((s) =>
          s.stage === event.stage
            ? {
                ...s,
                status: "completed" as const,
                summary: event.summary,
                finished_at: s.finished_at ?? now,
              }
            : s,
        ),
      };
    case "claim_verified":
      return {
        ...detail,
        claims: detail.claims.map((claim) =>
          claim.id === event.claim_id
            ? { ...claim, verdict: event.verdict }
            : claim,
        ),
      };
    case "done":
      return {
        ...detail,
        status: "completed",
        finished_at: detail.finished_at ?? now,
      };
    case "error":
      return {
        ...detail,
        status: "failed",
        error: event.message,
        finished_at: detail.finished_at ?? now,
      };
    case "stage_progress":
      // Progress lines live in the hook's ring buffer, never the cache.
      return detail;
  }
}

/** A server-sent message (has `data`) vs an EventSource transport error. */
function isServerEvent(event: Event): event is MessageEvent<string> {
  return typeof (event as MessageEvent).data === "string";
}

/**
 * Live analysis hook: TanStack snapshot + SSE merge.
 *
 * - Fetches the snapshot; while status is pending/running, opens
 *   `GET /api/analyses/{id}/events?after=<last_seq>` (see ANALYSIS_EVENTS_BASE
 *   in api.ts for the rewrite-vs-direct base).
 * - Events mutate the cached snapshot via a pure reducer; `claim_verified`
 *   additionally schedules a coalesced snapshot refetch because the event
 *   payload carries no evidence rows.
 * - `stage_progress` lines go to an in-hook ring buffer (last 200) returned
 *   as `activity` — they are display-only and never enter the cache.
 * - Out-of-order/duplicate guard: every event id is job_event.seq; events at
 *   or below the highest applied seq are dropped.
 * - Transport error: close, refetch snapshot, reopen after its `last_seq`
 *   (exponential backoff, 1s -> 15s). Terminal events close the stream and
 *   trigger one final refetch.
 */
export function useAnalysis(id: number) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.analysis(id),
    queryFn: () => api.getAnalysis(id),
    enabled: Number.isFinite(id),
  });

  const status = query.data?.status;
  const isLive = status === "pending" || status === "running";

  // Highest seq applied (snapshot or event) — the resume cursor + order
  // guard. Keyed by analysis id so navigating between analyses resets it.
  const seqRef = useRef({ id, seq: 0 });
  const snapshotSeq = query.data?.last_seq ?? 0;
  // Declared before the SSE effect so a fresh snapshot bumps the cursor first.
  useEffect(() => {
    if (seqRef.current.id !== id) seqRef.current = { id, seq: 0 };
    seqRef.current.seq = Math.max(seqRef.current.seq, snapshotSeq);
  }, [id, snapshotSeq]);

  // stage_progress ring buffer, keyed by id: lines from a previous analysis
  // are dropped on navigation without needing a reset effect.
  const [activityState, setActivityState] = useState<{
    id: number;
    lines: AnalysisActivityLine[];
  }>({ id, lines: [] });
  const activity = activityState.id === id ? activityState.lines : NO_ACTIVITY;

  useEffect(() => {
    if (!isLive || !Number.isFinite(id)) return;

    let disposed = false;
    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let refetchTimer: ReturnType<typeof setTimeout> | undefined;
    let backoffMs = 1_000;

    const invalidateSnapshot = () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.analysis(id) });

    /** Coalesces refetches across a burst of claim_verified events. */
    const scheduleSnapshotRefetch = () => {
      if (refetchTimer !== undefined) return;
      refetchTimer = setTimeout(() => {
        refetchTimer = undefined;
        void invalidateSnapshot();
      }, 400);
    };

    const handleEvent = (raw: MessageEvent<string>) => {
      const seq = Number(raw.lastEventId);
      if (Number.isFinite(seq) && seq > 0) {
        if (seq <= seqRef.current.seq) return; // duplicate / out of order
        seqRef.current.seq = seq;
      }
      let payload: Record<string, unknown> = {};
      if (raw.data) {
        try {
          payload = JSON.parse(raw.data) as Record<string, unknown>;
        } catch {
          return; // malformed frame — the next snapshot refetch covers it
        }
      }
      const event = { ...payload, type: raw.type } as AnalysisEvent;

      if (event.type === "stage_progress") {
        const line: AnalysisActivityLine = {
          seq: Number.isFinite(seq) ? seq : 0,
          stage: event.stage,
          message: event.message,
        };
        setActivityState((prev) => ({
          id,
          lines:
            prev.id === id
              ? [...prev.lines, line].slice(-ACTIVITY_BUFFER_SIZE)
              : [line],
        }));
        return;
      }

      queryClient.setQueryData<AnalysisDetail>(queryKeys.analysis(id), (old) =>
        old ? applyAnalysisEvent(old, event) : old,
      );
      if (event.type === "claim_verified") {
        // The event carries claim_id + verdict only; evidence + reasoning
        // arrive with the snapshot.
        scheduleSnapshotRefetch();
      }
      if (event.type === "done" || event.type === "error") {
        source?.close();
        source = null;
        void invalidateSnapshot();
      }
    };

    const open = () => {
      if (disposed) return;
      const es = new EventSource(api.analysisEventsUrl(id, seqRef.current.seq));
      source = es;
      es.onopen = () => {
        backoffMs = 1_000;
      };
      for (const name of ANALYSIS_EVENT_NAMES) {
        if (name === "error") continue; // handled below (transport collision)
        es.addEventListener(name, (raw) => handleEvent(raw as MessageEvent<string>));
      }
      // "error" is both our terminal server event AND the EventSource
      // transport-failure event; a `data` payload distinguishes them.
      es.addEventListener("error", (raw) => {
        if (isServerEvent(raw)) {
          handleEvent(raw);
          return;
        }
        // Transport drop: close, refetch the snapshot, resume after last_seq.
        es.close();
        if (source === es) source = null;
        if (disposed) return;
        const delay = backoffMs;
        backoffMs = Math.min(backoffMs * 2, 15_000);
        reconnectTimer = setTimeout(() => {
          void invalidateSnapshot().finally(() => {
            if (!disposed) open();
          });
        }, delay);
      });
    };

    open();
    return () => {
      disposed = true;
      source?.close();
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      if (refetchTimer !== undefined) clearTimeout(refetchTimer);
    };
  }, [id, isLive, queryClient]);

  return { query, activity, isLive };
}

// --------------------------------------------------------------------------
// Contradictions (Phase 3)
// --------------------------------------------------------------------------

export function useContradictions(params: ContradictionListParams) {
  return useQuery({
    queryKey: queryKeys.contradictionList(params),
    queryFn: () => api.listContradictions(params),
    placeholderData: keepPreviousData,
  });
}

/**
 * Row-expand evidence detail. `retry: false` because a backend that hasn't
 * shipped the detail endpoint yet should degrade immediately, not after
 * retries; the row falls back to counts-only.
 */
export function useContradiction(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.contradiction(id),
    queryFn: () => api.getContradiction(id),
    enabled: enabled && Number.isFinite(id),
    retry: false,
  });
}

/** Open-contradiction count for the sidebar badge; errors just mean no badge. */
export function useOpenContradictionCount() {
  return useQuery({
    queryKey: queryKeys.contradictionOpenCount,
    queryFn: () => api.listContradictions({ status: "open", page: 1, page_size: 1 }),
    select: (page) => page.total,
    refetchInterval: 60_000,
    retry: false,
  });
}

export function useDismissContradiction() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.dismissContradiction(id),
    onSuccess: () => {
      // Covers every list filter, the expanded detail, and the badge count.
      void queryClient.invalidateQueries({ queryKey: queryKeys.contradictions });
    },
  });
}

// --------------------------------------------------------------------------
// Investigations (Phase 4)
// --------------------------------------------------------------------------

export function useInvestigations(params: InvestigationListParams) {
  return useQuery({
    queryKey: queryKeys.investigationList(params),
    queryFn: () => api.listInvestigations(params),
    placeholderData: keepPreviousData,
    // Keep list rows fresh while any visible investigation is in flight.
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) => item.status === "pending" || item.status === "running",
      )
        ? 5_000
        : false,
  });
}

export function useCreateInvestigation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: InvestigationCreate) =>
      api.createInvestigation(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.investigations });
    },
  });
}

export function useCancelInvestigation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.cancelInvestigation(id),
    onSuccess: (_void, id) => {
      // 202 — cancellation is async; refetch so the status flips when it lands.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.investigation(id),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.investigations });
    },
  });
}

/** Flips a private investigation to shared — see useShareAnalysis. */
export function useShareInvestigation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, confirm }: ShareDossierInput) =>
      api.setInvestigationVisibility(id, "shared", confirm),
    onSuccess: (_result, { id }) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.investigation(id),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.investigations });
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
    },
  });
}

/**
 * Manual question recursion. `parentInvestigationId` lets the hook refresh
 * the parent snapshot so the question row picks up its spawned_dossier_id.
 */
export function useInvestigateQuestion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ questionId }: { questionId: number; parentInvestigationId?: number }) =>
      api.investigateQuestion(questionId),
    onSuccess: (_accepted, { parentInvestigationId }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.investigations });
      if (parentInvestigationId !== undefined) {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.investigation(parentInvestigationId),
        });
      }
    },
  });
}

/** One activity line for the investigation rail (ring buffer, never cached). */
export interface InvestigationActivityLine {
  seq: number;
  message: string;
}

/**
 * Human one-liner for the ActivityLog. Returns null for events that carry no
 * useful progress text (they still hit the reducer / refetch path).
 */
function investigationActivityMessage(event: InvestigationEvent): string | null {
  switch (event.type) {
    case "stage_progress":
      return event.message;
    case "iteration": {
      const tools =
        event.tools && event.tools.length > 0
          ? event.tools.join(", ")
          : "thinking";
      const cost =
        typeof event.cost_so_far === "number"
          ? ` · $${event.cost_so_far.toFixed(2)}`
          : "";
      return `#${event.n} ${tools}${cost}`;
    }
    case "finding_recorded": {
      const kind = event.kind ? ` (${event.kind}` : "";
      const spec = event.kind ? `${event.speculation ? ", hypothesis" : ""})` : "";
      return `finding f${event.finding_id} recorded${kind}${spec}`;
    }
    case "question_raised":
      return `question raised: ${event.text ?? event.qtype ?? `#${event.question_id}`}`;
    case "question_resolved":
      return `question #${event.question_id} ${event.status ?? "resolved"}`;
    case "doc_ingested":
      return `ingested: ${event.title ?? `document #${event.document_id}`}`;
    case "section_completed":
      return `section ready: ${event.section.replace(/_/g, " ")}`;
    default:
      return null;
  }
}

/**
 * Pure reducer applying one SSE event to the cached snapshot. Only fields the
 * thin event payloads can authoritatively patch are touched (stage statuses,
 * cost ticker, question status flips, terminal status); everything else
 * arrives via the coalesced snapshot refetch.
 */
function applyInvestigationEvent(
  detail: InvestigationDetail,
  event: InvestigationEvent,
): InvestigationDetail {
  const now = new Date().toISOString();
  switch (event.type) {
    case "stage_started": {
      const known = detail.stages.some((s) => s.stage === event.stage);
      const stages = known
        ? detail.stages.map((s) =>
            s.stage === event.stage
              ? { ...s, status: "running" as const, started_at: s.started_at ?? now }
              : s,
          )
        : [
            ...detail.stages,
            {
              stage: event.stage,
              status: "running" as const,
              summary: null,
              started_at: now,
              finished_at: null,
            },
          ];
      return { ...detail, status: "running", stages };
    }
    case "stage_completed":
      return {
        ...detail,
        stages: detail.stages.map((s) =>
          s.stage === event.stage
            ? {
                ...s,
                status: "completed" as const,
                summary: event.summary ?? s.summary,
                finished_at: s.finished_at ?? now,
              }
            : s,
        ),
      };
    case "iteration":
      return typeof event.cost_so_far === "number"
        ? { ...detail, cost_usd: event.cost_so_far }
        : detail;
    case "question_resolved":
      return event.status
        ? {
            ...detail,
            questions: detail.questions.map((q) =>
              q.id === event.question_id ? { ...q, status: event.status! } : q,
            ),
          }
        : detail;
    case "done":
      return {
        ...detail,
        status: "completed",
        finished_at: detail.finished_at ?? now,
      };
    case "error":
      return {
        ...detail,
        status: "failed",
        error: event.message,
        finished_at: detail.finished_at ?? now,
      };
    default:
      // finding_recorded / question_raised / doc_ingested / section_completed
      // / stage_progress: persisted rows arrive with the snapshot refetch.
      return detail;
  }
}

/** Events whose payloads are thinner than the rows they announce. */
const INVESTIGATION_REFETCH_EVENTS: ReadonlySet<InvestigationEvent["type"]> =
  new Set([
    "finding_recorded",
    "question_raised",
    "question_resolved",
    "doc_ingested",
    "section_completed",
    "stage_completed",
  ]);

/**
 * Live investigation hook — a clone of useAnalysis's snapshot+SSE reducer
 * against the investigation contract:
 *
 * - Snapshot via GET /api/investigations/{id}; while pending/running, opens
 *   `GET /api/investigations/{id}/events?after=<last_seq>`.
 * - Iteration lines (tool briefs + cost ticker), stage_progress, and row
 *   announcements feed an in-hook ring buffer (last 200) returned as
 *   `activity`; they never enter the query cache.
 * - Data-bearing events schedule a coalesced snapshot refetch (the payloads
 *   are thin); stage/cost/status fields are patched optimistically.
 * - Out-of-order/duplicate guard on job_event.seq; transport errors close the
 *   stream, refetch the snapshot, and resume after last_seq with backoff.
 */
export function useInvestigation(id: number) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.investigation(id),
    queryFn: () => api.getInvestigation(id),
    enabled: Number.isFinite(id),
  });

  const status = query.data?.status;
  const isLive = status === "pending" || status === "running";

  // Highest seq applied (snapshot or event) — resume cursor + order guard.
  const seqRef = useRef({ id, seq: 0 });
  const snapshotSeq = query.data?.last_seq ?? 0;
  useEffect(() => {
    if (seqRef.current.id !== id) seqRef.current = { id, seq: 0 };
    seqRef.current.seq = Math.max(seqRef.current.seq, snapshotSeq);
  }, [id, snapshotSeq]);

  const [activityState, setActivityState] = useState<{
    id: number;
    lines: InvestigationActivityLine[];
  }>({ id, lines: [] });
  const activity =
    activityState.id === id ? activityState.lines : NO_INVESTIGATION_ACTIVITY;

  useEffect(() => {
    if (!isLive || !Number.isFinite(id)) return;

    let disposed = false;
    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let refetchTimer: ReturnType<typeof setTimeout> | undefined;
    let backoffMs = 1_000;

    const invalidateSnapshot = () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.investigation(id) });

    /** Coalesces refetches across a burst of finding/question events. */
    const scheduleSnapshotRefetch = () => {
      if (refetchTimer !== undefined) return;
      refetchTimer = setTimeout(() => {
        refetchTimer = undefined;
        void invalidateSnapshot();
      }, 400);
    };

    const handleEvent = (raw: MessageEvent<string>) => {
      const seq = Number(raw.lastEventId);
      if (Number.isFinite(seq) && seq > 0) {
        if (seq <= seqRef.current.seq) return; // duplicate / out of order
        seqRef.current.seq = seq;
      }
      let payload: Record<string, unknown> = {};
      if (raw.data) {
        try {
          payload = JSON.parse(raw.data) as Record<string, unknown>;
        } catch {
          return; // malformed frame — the next snapshot refetch covers it
        }
      }
      const event = { ...payload, type: raw.type } as InvestigationEvent;

      const message = investigationActivityMessage(event);
      if (message !== null) {
        const line: InvestigationActivityLine = {
          seq: Number.isFinite(seq) ? seq : 0,
          message,
        };
        setActivityState((prev) => ({
          id,
          lines:
            prev.id === id
              ? [...prev.lines, line].slice(-ACTIVITY_BUFFER_SIZE)
              : [line],
        }));
      }

      queryClient.setQueryData<InvestigationDetail>(
        queryKeys.investigation(id),
        (old) => (old ? applyInvestigationEvent(old, event) : old),
      );
      if (INVESTIGATION_REFETCH_EVENTS.has(event.type)) {
        scheduleSnapshotRefetch();
      }
      if (event.type === "done" || event.type === "error") {
        source?.close();
        source = null;
        void invalidateSnapshot();
        // A finished run also updates the list rows (counts, cost, status).
        void queryClient.invalidateQueries({
          queryKey: queryKeys.investigations,
        });
      }
    };

    const open = () => {
      if (disposed) return;
      const es = new EventSource(
        api.investigationEventsUrl(id, seqRef.current.seq),
      );
      source = es;
      es.onopen = () => {
        backoffMs = 1_000;
      };
      for (const name of INVESTIGATION_EVENT_NAMES) {
        if (name === "error") continue; // handled below (transport collision)
        es.addEventListener(name, (raw) =>
          handleEvent(raw as MessageEvent<string>),
        );
      }
      // "error" is both our terminal server event AND the EventSource
      // transport-failure event; a `data` payload distinguishes them.
      es.addEventListener("error", (raw) => {
        if (isServerEvent(raw)) {
          handleEvent(raw);
          return;
        }
        // Transport drop: close, refetch the snapshot, resume after last_seq.
        es.close();
        if (source === es) source = null;
        if (disposed) return;
        const delay = backoffMs;
        backoffMs = Math.min(backoffMs * 2, 15_000);
        reconnectTimer = setTimeout(() => {
          void invalidateSnapshot().finally(() => {
            if (!disposed) open();
          });
        }, delay);
      });
    };

    open();
    return () => {
      disposed = true;
      source?.close();
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      if (refetchTimer !== undefined) clearTimeout(refetchTimer);
    };
  }, [id, isLive, queryClient]);

  return { query, activity, isLive };
}

/** Stable empty buffer returned while activity state belongs to another id. */
const NO_INVESTIGATION_ACTIVITY: InvestigationActivityLine[] = [];

// --------------------------------------------------------------------------
// Admin (v0.2 Phase D) — users / invites / settings / system spend
// --------------------------------------------------------------------------

// All admin reads use `retry: false`: the interesting failures are 403 (not
// an admin) and 404 (backend hasn't shipped the endpoint yet) and both
// should degrade immediately, not after a retry storm.

export function useAdminUsers() {
  return useQuery({
    queryKey: queryKeys.adminUsers,
    queryFn: api.listAdminUsers,
    retry: false,
  });
}

/** Role / disable / budget-override PATCH. Caps feed the spend dashboard
 *  and (when editing yourself) the topbar badge, so both refresh. */
export function useUpdateAdminUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: AdminUserUpdate }) =>
      api.updateAdminUser(id, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers });
      void queryClient.invalidateQueries({ queryKey: ["admin", "spend"] });
      void queryClient.invalidateQueries({ queryKey: ["spend"] });
    },
  });
}

export function useInvites() {
  return useQuery({
    queryKey: queryKeys.adminInvites,
    queryFn: api.listInvites,
    retry: false,
  });
}

export function useCreateInvite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: InviteCreate) => api.createInvite(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminInvites });
    },
  });
}

export function useDeleteInvite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (email: string) => api.deleteInvite(email),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminInvites });
    },
  });
}

export function useAdminSettings() {
  return useQuery({
    queryKey: queryKeys.adminSettings,
    queryFn: api.getAdminSettings,
    retry: false,
  });
}

/** Global budget knobs (app_setting wins over env). Member caps may change,
 *  so my-spend and the dashboard refresh alongside the settings. */
export function useUpdateAdminSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: AdminSettingsUpdate) =>
      api.updateAdminSettings(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminSettings });
      void queryClient.invalidateQueries({ queryKey: ["admin", "spend"] });
      void queryClient.invalidateQueries({ queryKey: ["spend"] });
    },
  });
}

export function useAdminSpend(days = 7) {
  return useQuery({
    queryKey: queryKeys.adminSpend(days),
    queryFn: () => api.getAdminSpend(days),
    refetchInterval: 60_000,
    retry: false,
  });
}
