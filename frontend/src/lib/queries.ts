"use client";

/**
 * TanStack Query hooks over the typed API client. Components only ever talk
 * to these hooks; mutations invalidate the query keys they affect.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import * as api from "@/lib/api";
import type {
  DocumentListParams,
  EntityListParams,
  FeedParams,
  IngestResult,
  IngestText,
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
  sources: ["sources"] as const,
  documents: ["documents"] as const,
  documentList: (params: DocumentListParams) =>
    ["documents", "list", params] as const,
  document: (id: number) => ["documents", "detail", id] as const,
  feed: (params: FeedParams) => ["feed", params] as const,
  watches: ["watches"] as const,
  watchBadges: ["watches", "badges"] as const,
  entities: ["entities"] as const,
  entityList: (params: EntityListParams) => ["entities", "list", params] as const,
  entity: (id: number) => ["entities", "detail", id] as const,
  entityDocuments: (id: number, params: { page?: number; page_size?: number }) =>
    ["entities", "detail", id, "documents", params] as const,
  search: (q: string, kind: SearchKind) => ["search", kind, q] as const,
  spend: (days: number) => ["spend", days] as const,
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

export function useSpend(days = 7) {
  return useQuery({
    queryKey: queryKeys.spend(days),
    queryFn: () => api.getSpend(days),
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
