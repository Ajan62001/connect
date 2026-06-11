"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { UseQueryResult } from "@tanstack/react-query";
import { format, subDays } from "date-fns";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  CalendarOffIcon,
  EyeIcon,
  FlaskConicalIcon,
  GitBranchIcon,
  LightbulbIcon,
  Loader2Icon,
  ScaleIcon,
  TrendingUpIcon,
  ZapIcon,
  type LucideIcon,
} from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import type { Brief, BriefItem, BriefSectionKey } from "@/lib/api";
import { formatDay, parseDateOnly, relativeTime } from "@/lib/format";
import { useCreateAnalysis, useMarkBriefItemSeen } from "@/lib/queries";
import { cn } from "@/lib/utils";

interface SectionConfig {
  key: BriefSectionKey;
  title: string;
  description: string;
  icon: LucideIcon;
}

/** The six brief sections, in display order — all always present in the API. */
const SECTIONS: SectionConfig[] = [
  {
    key: "watch_dev",
    title: "Watch developments",
    description: "New activity on your watched entities and topics.",
    icon: EyeIcon,
  },
  {
    key: "thread_move",
    title: "Story threads that moved",
    description: "Threads that gained events since the last brief.",
    icon: GitBranchIcon,
  },
  {
    key: "position_shift",
    title: "Position shifts",
    description:
      "Speakers whose stated position on a topic shifted or reversed since the last brief.",
    icon: ZapIcon,
  },
  {
    key: "contradiction",
    title: "Contradictions",
    description: "Claims your sources disagree on, new since the last brief.",
    icon: ScaleIcon,
  },
  {
    key: "trending_claim",
    title: "Trending claims",
    description: "Claims sighted across several sources within 48 hours.",
    icon: TrendingUpIcon,
  },
  {
    key: "suggestion",
    title: "Suggested analyses",
    description:
      "Deterministically scored candidates for a deep dive — reasons are template-rendered from score components.",
    icon: LightbulbIcon,
  },
];

// ---------------------------------------------------------------------------
// Payload helpers — `payload` is a loose object of denormalized display
// fields; read defensively so a missing key degrades, never crashes.
// ---------------------------------------------------------------------------

function payloadString(
  payload: Record<string, unknown>,
  ...keys: string[]
): string | null {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function payloadNumber(
  payload: Record<string, unknown>,
  ...keys: string[]
): number | null {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

function itemTitle(item: BriefItem): string {
  return (
    payloadString(item.payload, "title", "label", "text", "claim_text") ??
    `${item.object_type} #${item.object_id}`
  );
}

/** Where the row links to; claims have no page of their own (Analyze instead). */
function itemHref(item: BriefItem): string | null {
  switch (item.object_type) {
    case "event":
      return `/event/${item.object_id}`;
    case "thread":
      return `/thread/${item.object_id}`;
    case "document":
      return `/documents/${item.object_id}`;
    case "contradiction":
      return "/contradictions";
    case "claim":
      return null;
    case "position_shift":
      // Rendered by PositionShiftRow, which links the speaker's entity page.
      return null;
  }
}

/** Compact meta bits under the title: date · source · counts. */
function itemMeta(item: BriefItem): string[] {
  const bits: string[] = [];
  const date = payloadString(
    item.payload,
    "occurred_on",
    "date",
    "published_at",
  );
  if (date) bits.push(formatDay(date));
  const source = payloadString(item.payload, "source", "source_name");
  if (source) bits.push(source);
  const events = payloadNumber(item.payload, "event_count", "new_events");
  if (events !== null) bits.push(`+${plural(events, "event")}`);
  const docs = payloadNumber(
    item.payload,
    "doc_count",
    "document_count",
    "new_documents",
  );
  if (docs !== null) bits.push(plural(docs, "document"));
  const sources = payloadNumber(item.payload, "source_count");
  if (sources !== null) bits.push(plural(sources, "source"));
  return bits;
}

// ---------------------------------------------------------------------------
// Rows / sections
// ---------------------------------------------------------------------------

function SeenDot({ item }: { item: BriefItem }) {
  const markSeen = useMarkBriefItemSeen();
  return (
    <button
      type="button"
      disabled={item.seen}
      aria-label={item.seen ? "Seen" : "Mark as seen"}
      title={item.seen ? undefined : "Mark as seen"}
      onClick={() => markSeen.mutate(item.id)}
      className={cn(
        "mt-1.5 size-2.5 shrink-0 rounded-full transition-colors",
        item.seen
          ? "bg-transparent ring-1 ring-border ring-inset"
          : "cursor-pointer bg-primary hover:bg-primary/60",
      )}
    />
  );
}

/**
 * The claim text an [Analyze] button submits to POST /api/analyses — same
 * defensive payload reading as the row title.
 */
function analyzableText(item: BriefItem): string | null {
  return payloadString(item.payload, "text", "claim_text", "title", "label");
}

/** [Analyze] on suggestion rows: seed the pipeline with the suggested claim. */
function AnalyzeButton({ item }: { item: BriefItem }) {
  const router = useRouter();
  const createAnalysis = useCreateAnalysis();
  const text = analyzableText(item);
  if (!text) return null;

  return (
    <Button
      variant="outline"
      size="xs"
      className="shrink-0"
      disabled={createAnalysis.isPending}
      onClick={() =>
        createAnalysis.mutate(
          { input_text: text },
          {
            onSuccess: (accepted) =>
              router.push(`/analysis/${accepted.analysis_id}`),
            onError: (error) =>
              toast.error("Could not start analysis", {
                description: error.message,
              }),
          },
        )
      }
    >
      {createAnalysis.isPending ? (
        <Loader2Icon className="animate-spin" data-icon="inline-start" />
      ) : (
        <FlaskConicalIcon data-icon="inline-start" />
      )}
      Analyze
    </Button>
  );
}

/** Kind badge colorways — reversed reads stronger than shifted. */
const SHIFT_KIND_STYLES: Record<string, string> = {
  reversed: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  shifted: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
};

/** One side of the was/now quote pair on a position_shift card. */
function ShiftQuote({
  label,
  quote,
  date,
}: {
  label: string;
  quote: string | null;
  date: string | null;
}) {
  return (
    <div className="min-w-0 space-y-1 rounded-lg border bg-card p-2.5">
      <p className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
        {date ? ` · ${formatDay(date)}` : ""}
      </p>
      {quote ? (
        <blockquote className="line-clamp-3 border-l-2 border-border pl-2 text-xs leading-5 text-muted-foreground">
          &ldquo;{quote}&rdquo;
        </blockquote>
      ) : (
        <p className="text-xs text-muted-foreground">Quote unavailable.</p>
      )}
    </div>
  );
}

/**
 * position_shift rows get their own card body: entity name, topic, kind
 * badge, and the two verbatim quotes side-by-side with their dates. All
 * payload reads are defensive — a missing key degrades, never crashes.
 */
function PositionShiftRow({ item }: { item: BriefItem }) {
  const entityId = payloadNumber(item.payload, "entity_id");
  const entityName =
    payloadString(item.payload, "entity_name", "name") ??
    (entityId !== null ? `Entity #${entityId}` : "Unknown speaker");
  const topic = payloadString(item.payload, "topic");
  const kind = payloadString(item.payload, "kind");
  const fromQuote = payloadString(item.payload, "from_quote");
  const toQuote = payloadString(item.payload, "to_quote");
  const fromDate = payloadString(item.payload, "from_date");
  const toDate = payloadString(item.payload, "to_date");

  return (
    <li className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
      <SeenDot item={item} />
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {entityId !== null ? (
            <Link
              href={`/entity/${entityId}`}
              className="text-sm font-medium underline-offset-4 hover:underline"
            >
              {entityName}
            </Link>
          ) : (
            <span className="text-sm font-medium">{entityName}</span>
          )}
          {topic ? <Badge variant="secondary">{topic}</Badge> : null}
          {kind ? (
            <Badge
              variant="secondary"
              className={SHIFT_KIND_STYLES[kind] ?? ""}
            >
              <ZapIcon aria-hidden />
              {kind}
            </Badge>
          ) : null}
        </div>
        {fromQuote !== null || toQuote !== null ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <ShiftQuote label="Was" quote={fromQuote} date={fromDate} />
            <ShiftQuote label="Now" quote={toQuote} date={toDate} />
          </div>
        ) : null}
        {item.reason ? (
          <p className="text-xs text-muted-foreground/80 italic">
            {item.reason}
          </p>
        ) : null}
      </div>
    </li>
  );
}

function BriefItemRow({ item }: { item: BriefItem }) {
  const href = itemHref(item);
  const title = itemTitle(item);
  const eventType = payloadString(item.payload, "event_type");
  const meta = itemMeta(item);

  return (
    <li className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
      <SeenDot item={item} />
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {href ? (
            <Link
              href={href}
              className="text-sm font-medium underline-offset-4 hover:underline"
            >
              {title}
            </Link>
          ) : (
            <span className="text-sm font-medium">{title}</span>
          )}
          {eventType ? (
            <Badge variant="outline">{eventType.replace(/_/g, " ")}</Badge>
          ) : null}
        </div>
        {meta.length > 0 ? (
          <p className="text-xs text-muted-foreground">{meta.join(" · ")}</p>
        ) : null}
        {item.reason ? (
          <p className="text-xs text-muted-foreground/80 italic">
            {item.reason}
          </p>
        ) : null}
      </div>
      {item.section === "suggestion" ? <AnalyzeButton item={item} /> : null}
    </li>
  );
}

function BriefSectionCard({
  config,
  items,
}: {
  config: SectionConfig;
  items: BriefItem[];
}) {
  const Icon = config.icon;
  const ordered = [...items].sort((a, b) => a.rank - b.rank);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Icon className="size-4 text-muted-foreground" aria-hidden />
          {config.title}
          {ordered.length > 0 ? (
            <Badge variant="secondary">{ordered.length}</Badge>
          ) : null}
        </CardTitle>
        <CardDescription>{config.description}</CardDescription>
      </CardHeader>
      <CardContent>
        {ordered.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing new today.</p>
        ) : (
          <ul className="divide-y">
            {ordered.map((item) =>
              item.section === "position_shift" ? (
                <PositionShiftRow key={item.id} item={item} />
              ) : (
                <BriefItemRow key={item.id} item={item} />
              ),
            )}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function BriefSkeleton() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-7 w-40" />
        <Skeleton className="h-4 w-72" />
      </div>
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-36 w-full rounded-xl" />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// BriefView — shared by /today and /brief/[date]
// ---------------------------------------------------------------------------

export function BriefView({ query }: { query: UseQueryResult<Brief, Error> }) {
  if (query.isPending) return <BriefSkeleton />;

  if (query.isError) {
    // A past day with no persisted brief is a normal state, not a failure.
    if (query.error instanceof ApiError && query.error.status === 404) {
      return (
        <>
          <PageHeader title="Brief" />
          <EmptyState
            icon={CalendarOffIcon}
            title="No brief for this day"
            description="Briefs are generated on days connect is opened; this one never was."
          />
        </>
      );
    }
    return (
      <>
        <PageHeader title="Brief" />
        <QueryError error={query.error} onRetry={() => void query.refetch()} />
      </>
    );
  }

  const { brief, sections } = query.data;
  const briefDate = parseDateOnly(brief.brief_date);
  const prevDate = briefDate ? format(subDays(briefDate, 1), "yyyy-MM-dd") : null;
  const isToday = brief.brief_date === format(new Date(), "yyyy-MM-dd");

  return (
    <>
      <PageHeader
        title={isToday ? "Today" : formatDay(brief.brief_date)}
        description={[
          briefDate ? format(briefDate, "EEEE, d MMMM yyyy") : brief.brief_date,
          `generated ${relativeTime(brief.generated_at)}`,
        ].join(" · ")}
        actions={
          <>
            {prevDate ? (
              <Button
                variant="ghost"
                size="sm"
                render={<Link href={`/brief/${prevDate}`} />}
              >
                <ArrowLeftIcon data-icon="inline-start" />
                {isToday ? "Yesterday" : "Previous day"}
              </Button>
            ) : null}
            {!isToday ? (
              <Button variant="ghost" size="sm" render={<Link href="/today" />}>
                Today
                <ArrowRightIcon data-icon="inline-end" />
              </Button>
            ) : null}
          </>
        }
      />
      {SECTIONS.map((section) => (
        <BriefSectionCard
          key={section.key}
          config={section}
          items={sections[section.key] ?? []}
        />
      ))}
    </>
  );
}
