"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FlaskConicalIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import {
  AnalysisStatusChip,
  VerdictSummaryChips,
} from "@/components/analysis/VerdictBadge";
import { EmptyState } from "@/components/shared/EmptyState";
import { Paginator } from "@/components/shared/Paginator";
import { QueryError } from "@/components/shared/QueryError";
import { ScopeTabs } from "@/components/shared/ScopeTabs";
import { OwnerByline, VisibilityBadge, VisibilityToggle } from "@/components/shared/Visibility";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, matchesDossierScope } from "@/lib/api";
import type { AnalysisListItem, DossierScope, Visibility } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useAnalyses, useCreateAnalysis, useMe } from "@/lib/queries";

const PAGE_SIZE = 10;

function AnalysisRow({ item }: { item: AnalysisListItem }) {
  return (
    <li>
      <Link
        href={`/analysis/${item.id}`}
        className="flex flex-col gap-1.5 rounded-lg px-3 py-3 transition-colors hover:bg-muted/60"
      >
        <p className="line-clamp-2 text-sm font-medium">{item.input_text}</p>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <AnalysisStatusChip status={item.status} />
          <VisibilityBadge visibility={item.visibility} />
          {item.visibility === "shared" ? (
            <OwnerByline ownerId={item.owner_id} ownerName={item.owner_name} />
          ) : null}
          {item.verdict_summary ? (
            <VerdictSummaryChips summary={item.verdict_summary} />
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

function RecentAnalyses() {
  const [page, setPage] = useState(1);
  // Mine | Shared | All — the community feed lives on this list (design §6).
  const [scope, setScope] = useState<DossierScope>("all");
  const me = useMe();
  const analyses = useAnalyses({ page, page_size: PAGE_SIZE, scope });

  const tabs = (
    <div className="flex items-center justify-between gap-3">
      <h2 className="text-sm font-medium text-muted-foreground">
        Recent analyses
      </h2>
      <ScopeTabs
        value={scope}
        onChange={(next) => {
          setScope(next);
          setPage(1);
        }}
      />
    </div>
  );

  if (analyses.isPending) {
    return (
      <div className="space-y-3">
        {tabs}
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-16 w-full rounded-lg" />
        ))}
      </div>
    );
  }
  if (analyses.isError) {
    // A backend without Phase 3 endpoints yet is a graceful state, not a crash.
    if (analyses.error instanceof ApiError && analyses.error.status === 404) {
      return (
        <EmptyState
          icon={FlaskConicalIcon}
          title="Analysis API not available yet"
          description="The backend hasn't shipped the verification endpoints; this page will light up once it does."
        />
      );
    }
    return (
      <QueryError error={analyses.error} onRetry={() => void analyses.refetch()} />
    );
  }

  // Client half of the scope contract: a scope-aware backend already
  // filtered (this is a no-op); a scope-blind one paginates unfiltered,
  // so each page shows its matching subset.
  const items = analyses.data.items.filter((item) =>
    matchesDossierScope(item, scope, me.data?.id),
  );

  if (analyses.data.items.length === 0) {
    return (
      <div className="space-y-3">
        {tabs}
        <EmptyState
          icon={FlaskConicalIcon}
          title={scope === "all" ? "No analyses yet" : `No ${scope === "mine" ? "analyses of yours" : "shared analyses"} yet`}
          description="Paste a claim above to run the verification pipeline against your sources."
        />
      </div>
    );
  }
  return (
    <div className="space-y-4">
      {tabs}
      {items.length === 0 ? (
        <EmptyState
          icon={FlaskConicalIcon}
          title={
            scope === "mine"
              ? "None of your analyses on this page"
              : "No shared analyses on this page"
          }
          description="Pagination happens before this filter — try the next page or another tab."
        />
      ) : (
        <ul className="divide-y rounded-xl border bg-card">
          {items.map((item) => (
            <AnalysisRow key={item.id} item={item} />
          ))}
        </ul>
      )}
      <Paginator
        page={analyses.data.page}
        pageSize={analyses.data.page_size}
        total={analyses.data.total}
        onPageChange={setPage}
      />
    </div>
  );
}

export default function AnalyzePage() {
  const router = useRouter();
  const create = useCreateAnalysis();
  const [text, setText] = useState("");
  // Shared by default (design §1) — private is the deliberate opt-in.
  const [visibility, setVisibility] = useState<Visibility>("shared");

  const submit = () => {
    const input = text.trim();
    if (!input || create.isPending) return;
    create.mutate(
      { input_text: input, visibility },
      {
        onSuccess: (accepted) => {
          router.push(`/analysis/${accepted.analysis_id}`);
        },
        onError: (error) =>
          toast.error("Could not start analysis", { description: error.message }),
      },
    );
  };

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <Card className="mt-6">
        <CardHeader className="text-center">
          <CardTitle className="text-lg">Analyze a claim</CardTitle>
          <CardDescription>
            The pipeline decomposes the input into checkable claims, gathers
            evidence across your sources, and aggregates a verdict per claim.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste a claim or policy announcement..."
            className="min-h-28"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <VisibilityToggle
            value={visibility}
            onChange={setVisibility}
            disabled={create.isPending}
            kind="analysis"
          />
          <div className="flex items-center justify-end gap-3">
            <span className="text-xs text-muted-foreground">
              Ctrl/Cmd + Enter to submit
            </span>
            <Button disabled={!text.trim() || create.isPending} onClick={submit}>
              {create.isPending ? (
                <Loader2Icon className="animate-spin" data-icon="inline-start" />
              ) : (
                <FlaskConicalIcon data-icon="inline-start" />
              )}
              Analyze
            </Button>
          </div>
        </CardContent>
      </Card>

      <section className="space-y-3">
        <RecentAnalyses />
      </section>
    </div>
  );
}
