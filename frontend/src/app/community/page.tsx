"use client";

import { useState } from "react";
import Link from "next/link";
import { FlaskConicalIcon, TelescopeIcon, UsersIcon } from "lucide-react";

import { AnalysisStatusChip, VerdictSummaryChips } from "@/components/analysis/VerdictBadge";
import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { OwnerByline } from "@/components/shared/Visibility";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, matchesDossierScope } from "@/lib/api";
import type {
  AnalysisListItem,
  AnalysisStatus,
  InvestigationListItem,
} from "@/lib/api";
import { formatUsd, relativeTime } from "@/lib/format";
import { useAnalyses, useInvestigations, useMe } from "@/lib/queries";

/** How many of each dossier kind the merged stream considers. */
const FETCH_SIZE = 50;

/** One merged community-stream row (analysis or investigation). */
interface CommunityItem {
  kind: "analysis" | "investigation";
  id: number;
  href: string;
  title: string;
  status: AnalysisStatus;
  created_at: string;
  finished_at: string | null;
  owner_id: number | null | undefined;
  owner_name: string | null | undefined;
  analysis: AnalysisListItem | null;
  investigation: InvestigationListItem | null;
}

function toCommunityItem(
  item: AnalysisListItem | InvestigationListItem,
  kind: "analysis" | "investigation",
): CommunityItem {
  const isAnalysis = kind === "analysis";
  const analysis = isAnalysis ? (item as AnalysisListItem) : null;
  const investigation = !isAnalysis ? (item as InvestigationListItem) : null;
  return {
    kind,
    id: item.id,
    href: isAnalysis ? `/analysis/${item.id}` : `/investigation/${item.id}`,
    title: analysis
      ? analysis.input_text
      : (investigation?.title ?? `Investigation #${item.id}`),
    status: item.status,
    created_at: item.created_at,
    finished_at: item.finished_at,
    owner_id: item.owner_id,
    owner_name: item.owner_name,
    analysis,
    investigation,
  };
}

function CommunityRow({ item }: { item: CommunityItem }) {
  const investigation = item.investigation;
  const counts = investigation?.counts ?? null;
  return (
    <li>
      <Link
        href={item.href}
        className="flex flex-col gap-1.5 rounded-lg px-3 py-3 transition-colors hover:bg-muted/60"
      >
        <p className="line-clamp-2 text-sm font-medium">{item.title}</p>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <Badge variant="outline">
            {item.kind === "analysis" ? (
              <FlaskConicalIcon aria-hidden />
            ) : (
              <TelescopeIcon aria-hidden />
            )}
            {item.kind}
          </Badge>
          <AnalysisStatusChip status={item.status} />
          {/* Attribution chip — whose shared work this is. */}
          <OwnerByline ownerId={item.owner_id} ownerName={item.owner_name} />
          {item.analysis?.verdict_summary ? (
            <VerdictSummaryChips summary={item.analysis.verdict_summary} />
          ) : null}
          {counts && counts.findings > 0 ? (
            <Badge variant="secondary">
              {counts.findings} finding{counts.findings === 1 ? "" : "s"}
            </Badge>
          ) : null}
          {typeof investigation?.cost_usd === "number" ? (
            <span className="tabular-nums">
              {formatUsd(investigation.cost_usd)}
            </span>
          ) : null}
          <span className="ml-auto">
            {item.finished_at
              ? `finished ${relativeTime(item.finished_at)}`
              : `started ${relativeTime(item.created_at)}`}
          </span>
        </div>
      </Link>
    </li>
  );
}

/**
 * Recent shared dossiers across all members — analyses and investigations
 * merged into one stream (shared-by-default is the community compounding
 * point, design §1) with a by-member filter. Heavy lifting stays on the
 * existing list endpoints: this page just merges their `shared` scopes.
 */
export default function CommunityPage() {
  const me = useMe();
  // Server-side scope where supported; matchesDossierScope below keeps the
  // stream correct against a scope-blind backend (see DossierScope).
  const analyses = useAnalyses({ page: 1, page_size: FETCH_SIZE, scope: "shared" });
  const investigations = useInvestigations({
    page: 1,
    page_size: FETCH_SIZE,
    scope: "shared",
  });
  const [ownerFilter, setOwnerFilter] = useState<string>("all");

  const header = (
    <PageHeader
      title="Community"
      description="What members have shared — every dossier here is readable by everyone and its grounded findings compound the shared knowledge base."
    />
  );

  if (analyses.isPending || investigations.isPending) {
    return (
      <div className="space-y-4">
        {header}
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-16 w-full rounded-lg" />
        ))}
      </div>
    );
  }

  const notFound = (error: unknown) =>
    error instanceof ApiError && error.status === 404;
  if (analyses.isError || investigations.isError) {
    const errors = [analyses.error, investigations.error].filter(
      (error): error is Error => error instanceof Error,
    );
    if (errors.length > 0 && errors.every(notFound)) {
      return (
        <div className="space-y-4">
          {header}
          <EmptyState
            icon={UsersIcon}
            title="Community APIs not available yet"
            description="The backend hasn't shipped the dossier endpoints; shared work appears here once it does."
          />
        </div>
      );
    }
    const error = [analyses.error, investigations.error].find(
      (candidate): candidate is Error =>
        candidate instanceof Error && !notFound(candidate),
    );
    return (
      <div className="space-y-4">
        {header}
        <QueryError
          error={error ?? new Error("Could not load shared work")}
          onRetry={() => {
            void analyses.refetch();
            void investigations.refetch();
          }}
        />
      </div>
    );
  }

  const shared: CommunityItem[] = [
    ...analyses.data.items.map((item) => toCommunityItem(item, "analysis")),
    ...investigations.data.items.map((item) =>
      toCommunityItem(item, "investigation"),
    ),
  ]
    .filter((item) => matchesDossierScope(item, "shared", me.data?.id))
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));

  // By-member filter options, built from whoever actually shared something.
  const owners = new Map<number, string>();
  for (const item of shared) {
    if (item.owner_id == null) continue;
    const name =
      item.owner_id === me.data?.id
        ? "You"
        : (item.owner_name ?? `Member #${item.owner_id}`);
    owners.set(item.owner_id, name);
  }
  const ownerItems: Record<string, string> = { all: "All members" };
  for (const [id, name] of owners) ownerItems[String(id)] = name;

  const visible =
    ownerFilter === "all"
      ? shared
      : shared.filter((item) => String(item.owner_id) === ownerFilter);

  return (
    <div className="space-y-4">
      {header}

      {owners.size > 0 ? (
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            Latest {FETCH_SIZE} shared dossiers of each kind.
          </p>
          <Select
            items={ownerItems}
            value={ownerFilter}
            onValueChange={(value) => {
              if (typeof value === "string") setOwnerFilter(value);
            }}
          >
            <SelectTrigger size="sm" className="w-44">
              <SelectValue placeholder="All members" />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(ownerItems).map(([value, label]) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      ) : null}

      {visible.length === 0 ? (
        <EmptyState
          icon={UsersIcon}
          title={
            ownerFilter === "all"
              ? "Nothing shared yet"
              : "Nothing shared by this member"
          }
          description="Analyses and investigations are shared by default — run one, or flip a private dossier to shared from its detail page."
        />
      ) : (
        <ul className="divide-y rounded-xl border bg-card">
          {visible.map((item) => (
            <CommunityRow key={`${item.kind}-${item.id}`} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}
