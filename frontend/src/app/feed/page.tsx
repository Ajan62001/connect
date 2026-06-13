"use client";

import { useState } from "react";
import Link from "next/link";
import { InboxIcon, SparklesIcon } from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { Paginator } from "@/components/shared/Paginator";
import { QueryError } from "@/components/shared/QueryError";
import { StatusChip, WatchHitChip } from "@/components/shared/StatusChip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { DocumentListItem, EnrichmentStatus } from "@/lib/api";
import { absoluteTime, formatUsd, mediaTypeLabel, relativeTime, urlHost } from "@/lib/format";
import { useEnrichmentSweep, useFeed, useFeedFacets, useSpend } from "@/lib/queries";

const PAGE_SIZE = 25;
const SWEEP_LIMIT = 25;

/**
 * Manual sync enrichment sweep. Guarded by a confirm dialog (it spends real
 * money) and disabled when /api/spend is unreachable — no budget visibility,
 * no spending.
 */
function EnrichNowButton() {
  const spend = useSpend(7);
  const sweep = useEnrichmentSweep();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const disabled = spend.isError;
  // Sweeps are system-level spend: prefer the global envelope for the
  // confirm copy, falling back to my slice on a my-spend-only response.
  const sweepLedger = spend.data?.global ?? spend.data?.mine ?? null;

  function runSweep() {
    sweep.mutate(
      { mode: "sync", limit: SWEEP_LIMIT },
      {
        onSuccess: (job) => {
          setConfirmOpen(false);
          toast.success("Enrichment sweep started", {
            description: `Job #${job.job_id} is enriching up to ${SWEEP_LIMIT} pending documents.`,
          });
        },
        onError: (error) =>
          toast.error("Sweep failed to start", { description: error.message }),
      },
    );
  }

  const button = (
    <Button
      variant="outline"
      disabled={disabled || sweep.isPending}
      onClick={() => setConfirmOpen(true)}
    >
      <SparklesIcon data-icon="inline-start" />
      Enrich now
    </Button>
  );

  return (
    <>
      {disabled ? (
        <span title="Spend tracking is unavailable — enrichment is disabled until /api/spend responds.">
          {button}
        </span>
      ) : (
        button
      )}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Run enrichment sweep?</DialogTitle>
            <DialogDescription>
              Synchronously enrich up to {SWEEP_LIMIT} pending documents
              through the T1 ladder. This calls the Anthropic API and spends
              real money
              {sweepLedger
                ? ` (today: ${formatUsd(sweepLedger.today_usd)}${
                    sweepLedger.cap_usd !== null
                      ? ` of ${formatUsd(sweepLedger.cap_usd)} cap`
                      : ""
                  } used)`
                : ""}
              .
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button disabled={sweep.isPending} onClick={runSweep}>
              Enrich {SWEEP_LIMIT} documents
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

const STATUS_TABS: { value: EnrichmentStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "queued", label: "Queued" },
  { value: "done", label: "Done" },
  { value: "failed", label: "Failed" },
  { value: "skipped_dup", label: "Duplicates" },
  { value: "skipped_aged", label: "Aged out" },
];

function humanizeNewsType(type: string): string {
  const t = type.replace(/_/g, " ").trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

const MAX_TOPIC_TAGS = 4;

function FeedRow({ item }: { item: DocumentListItem }) {
  const topics = item.topics ?? [];
  const hasTags = Boolean(item.news_type) || topics.length > 0;
  return (
    <Link
      href={`/documents/${item.id}`}
      className="block rounded-xl border bg-card px-4 py-3 transition-colors hover:bg-muted/50"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-1">
          <p className="truncate text-sm font-medium">
            {item.title ?? "(untitled document)"}
          </p>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            {item.source_name ? <span>{item.source_name}</span> : null}
            {item.url ? <span>{urlHost(item.url)}</span> : null}
            {item.published_at ? (
              <span title={absoluteTime(item.published_at)}>
                published {item.published_at.slice(0, 10)}
              </span>
            ) : null}
            <span title={absoluteTime(item.fetched_at)}>
              fetched {relativeTime(item.fetched_at)}
            </span>
            {item.media_type ? <span>{mediaTypeLabel(item.media_type)}</span> : null}
          </p>
          {hasTags ? (
            <div className="flex flex-wrap items-center gap-1 pt-0.5">
              {item.news_type ? (
                <Badge variant="secondary">
                  {humanizeNewsType(item.news_type)}
                </Badge>
              ) : null}
              {topics.slice(0, MAX_TOPIC_TAGS).map((topic) => (
                <Badge key={topic} variant="outline">
                  {topic}
                </Badge>
              ))}
              {topics.length > MAX_TOPIC_TAGS ? (
                <span className="text-xs text-muted-foreground">
                  +{topics.length - MAX_TOPIC_TAGS}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {item.watch_hit ? <WatchHitChip /> : null}
          {item.canonical_document_id !== null ? (
            <Badge variant="outline">dup of #{item.canonical_document_id}</Badge>
          ) : null}
          <StatusChip status={item.enrichment_status} />
        </div>
      </div>
    </Link>
  );
}

function FeedSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="rounded-xl border bg-card px-4 py-3">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0 flex-1 space-y-2">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-3 w-1/3" />
            </div>
            <Skeleton className="h-5 w-16" />
          </div>
        </div>
      ))}
    </div>
  );
}

const ALL = "all";

export default function FeedPage() {
  const [status, setStatus] = useState<EnrichmentStatus | "all">("all");
  const [newsType, setNewsType] = useState<string>(ALL);
  const [topic, setTopic] = useState<string>(ALL);
  const [sourceId, setSourceId] = useState<string>(ALL);
  const [page, setPage] = useState(1);

  const facets = useFeedFacets();
  const feed = useFeed({
    ...(status === "all" ? {} : { status }),
    ...(newsType === ALL ? {} : { news_type: newsType }),
    ...(topic === ALL ? {} : { topic }),
    ...(sourceId === ALL ? {} : { source_id: Number(sourceId) }),
    page,
    page_size: PAGE_SIZE,
  });

  const onFilter = (setter: (v: string) => void) => (value: string | null) => {
    setter(value ?? ALL);
    setPage(1);
  };
  const hasFilters =
    newsType !== ALL || topic !== ALL || sourceId !== ALL || status !== "all";

  return (
    <>
      <PageHeader
        title="Feed"
        description="Everything entering the corpus, newest first — the pipeline-transparency view."
        actions={<EnrichNowButton />}
      />

      <Tabs
        value={status}
        onValueChange={(value) => {
          setStatus(value as EnrichmentStatus | "all");
          setPage(1);
        }}
      >
        <TabsList>
          {STATUS_TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex flex-wrap items-center gap-2">
        <Select value={newsType} onValueChange={onFilter(setNewsType)}>
          <SelectTrigger className="h-8 w-auto min-w-40 text-xs">
            <SelectValue placeholder="News type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All news types</SelectItem>
            {(facets.data?.news_types ?? []).map((t) => (
              <SelectItem key={t} value={t}>
                {humanizeNewsType(t)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select value={topic} onValueChange={onFilter(setTopic)}>
          <SelectTrigger className="h-8 w-auto min-w-40 text-xs">
            <SelectValue placeholder="Topic" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All topics</SelectItem>
            {(facets.data?.topics ?? []).map((t) => (
              <SelectItem key={t} value={t}>
                {t}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select value={sourceId} onValueChange={onFilter(setSourceId)}>
          <SelectTrigger className="h-8 w-auto min-w-40 text-xs">
            <SelectValue placeholder="Source" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All sources</SelectItem>
            {(facets.data?.sources ?? []).map((s) => (
              <SelectItem key={s.id} value={String(s.id)}>
                {s.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {hasFilters ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setStatus("all");
              setNewsType(ALL);
              setTopic(ALL);
              setSourceId(ALL);
              setPage(1);
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </div>

      {feed.isPending ? (
        <FeedSkeleton />
      ) : feed.isError ? (
        <QueryError error={feed.error} onRetry={() => void feed.refetch()} />
      ) : feed.data.items.length === 0 ? (
        <EmptyState
          icon={InboxIcon}
          title={status === "all" ? "Nothing in the feed yet" : "No matching items"}
          description={
            status === "all"
              ? "The RSS poller fills this automatically once sources are enabled — or use “Add to corpus” to ingest something now."
              : "No documents currently have this enrichment status."
          }
        />
      ) : (
        <>
          <div className="space-y-2">
            {feed.data.items.map((item) => (
              <FeedRow key={item.id} item={item} />
            ))}
          </div>
          <Paginator
            page={feed.data.page}
            pageSize={feed.data.page_size}
            total={feed.data.total}
            onPageChange={setPage}
          />
        </>
      )}
    </>
  );
}
