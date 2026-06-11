"use client";

import { use, useState } from "react";
import Link from "next/link";
import {
  ArrowLeftIcon,
  CircleAlertIcon,
  FileQuestionIcon,
  Loader2Icon,
  QuoteIcon,
} from "lucide-react";
import { toast } from "sonner";

import { StanceCard } from "@/components/analysis/StanceCard";
import { StageTimeline } from "@/components/analysis/StageTimeline";
import {
  AnalysisStatusChip,
  VerdictBadge,
} from "@/components/analysis/VerdictBadge";
import {
  DocumentSheet,
  type DocumentSheetTarget,
} from "@/components/documents/DocumentSheet";
import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { AnalysisClaim, AnalysisDetail } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useAnalysis, useCancelAnalysis } from "@/lib/queries";

function ClaimCard({
  claim,
  isLive,
  onOpenDocument,
}: {
  claim: AnalysisClaim;
  isLive: boolean;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <p className="min-w-0 flex-1 text-sm font-medium leading-6">
            {claim.text}
          </p>
          <span className="flex shrink-0 items-center gap-1.5">
            {claim.kind ? <Badge variant="outline">{claim.kind}</Badge> : null}
            {claim.verdict ? (
              <VerdictBadge verdict={claim.verdict} confidence={claim.confidence} />
            ) : !claim.checkable ? (
              <Badge variant="secondary" className="bg-muted text-muted-foreground">
                not checkable
              </Badge>
            ) : isLive ? (
              <Badge variant="secondary" className="bg-muted text-muted-foreground">
                <Loader2Icon className="animate-spin" aria-hidden />
                verifying
              </Badge>
            ) : (
              <VerdictBadge verdict="unverified" />
            )}
          </span>
        </div>
      </CardHeader>
      {claim.reasoning || claim.evidence.length > 0 ? (
        <CardContent className="space-y-4">
          {claim.reasoning ? (
            <p className="max-w-prose text-sm leading-6 text-muted-foreground">
              {claim.reasoning}
            </p>
          ) : null}
          {claim.evidence.length > 0 ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {claim.evidence.map((evidence) => (
                <StanceCard
                  key={evidence.id}
                  evidence={evidence}
                  onOpenDocument={onOpenDocument}
                />
              ))}
            </div>
          ) : null}
        </CardContent>
      ) : null}
    </Card>
  );
}

function ClaimsColumn({
  analysis,
  isLive,
  onOpenDocument,
}: {
  analysis: AnalysisDetail;
  isLive: boolean;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  if (analysis.claims.length === 0) {
    if (isLive) {
      return (
        <div className="space-y-4">
          <Skeleton className="h-28 w-full rounded-xl" />
          <Skeleton className="h-28 w-full rounded-xl" />
          <p className="text-center text-xs text-muted-foreground">
            Claims appear once the input has been decomposed.
          </p>
        </div>
      );
    }
    return (
      <EmptyState
        icon={QuoteIcon}
        title="No claims extracted"
        description="The normalize stage found nothing checkable in this input."
      />
    );
  }
  return (
    <div className="space-y-4">
      {analysis.claims.map((claim) => (
        <ClaimCard
          key={claim.id}
          claim={claim}
          isLive={isLive}
          onOpenDocument={onOpenDocument}
        />
      ))}
    </div>
  );
}

function AnalysisSkeleton() {
  return (
    <div className="flex flex-col gap-6 lg:flex-row">
      <Skeleton className="h-64 w-full shrink-0 rounded-xl lg:w-72" />
      <div className="flex-1 space-y-4">
        <Skeleton className="h-28 w-full rounded-xl" />
        <Skeleton className="h-28 w-full rounded-xl" />
      </div>
    </div>
  );
}

export default function AnalysisPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const analysisId = Number(id);
  const { query, activity, isLive } = useAnalysis(analysisId);
  const cancel = useCancelAnalysis();
  const [sheetTarget, setSheetTarget] = useState<DocumentSheetTarget | null>(null);

  if (!Number.isFinite(analysisId)) {
    return (
      <EmptyState
        icon={FileQuestionIcon}
        title="Invalid analysis id"
        description={`“${id}” is not an analysis id.`}
      />
    );
  }

  const onCancel = () =>
    cancel.mutate(analysisId, {
      onSuccess: () => toast.info("Cancellation requested"),
      onError: (error) =>
        toast.error("Could not cancel", { description: error.message }),
    });

  return (
    <>
      <Button variant="ghost" size="sm" render={<Link href="/analyze" />}>
        <ArrowLeftIcon data-icon="inline-start" />
        Analyses
      </Button>

      {query.isPending ? (
        <AnalysisSkeleton />
      ) : query.isError ? (
        <QueryError error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold tracking-tight">
                Analysis #{query.data.id}
              </h1>
              <AnalysisStatusChip status={query.data.status} />
              <span className="text-xs text-muted-foreground">
                started {relativeTime(query.data.created_at)}
                {query.data.finished_at
                  ? ` · finished ${relativeTime(query.data.finished_at)}`
                  : ""}
              </span>
            </div>
            <p className="max-w-prose text-sm leading-6 text-muted-foreground">
              {query.data.input_text}
            </p>
          </div>

          {query.data.status === "failed" && query.data.error ? (
            <div className="flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-4">
              <CircleAlertIcon
                className="mt-0.5 size-4 shrink-0 text-destructive"
                aria-hidden
              />
              <p className="text-sm text-destructive">{query.data.error}</p>
            </div>
          ) : null}

          <div className="flex flex-col gap-6 lg:flex-row">
            <aside className="w-full shrink-0 lg:w-72">
              <StageTimeline
                stages={query.data.stages}
                activity={activity}
                isLive={isLive}
                onCancel={onCancel}
                cancelling={cancel.isPending}
              />
            </aside>
            <div className="min-w-0 flex-1">
              <ClaimsColumn
                analysis={query.data}
                isLive={isLive}
                onOpenDocument={setSheetTarget}
              />
            </div>
          </div>
        </>
      )}

      <DocumentSheet
        target={sheetTarget}
        onOpenChange={(open) => {
          if (!open) setSheetTarget(null);
        }}
      />
    </>
  );
}
