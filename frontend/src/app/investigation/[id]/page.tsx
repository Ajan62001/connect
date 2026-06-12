"use client";

import { use, useState } from "react";
import Link from "next/link";
import {
  ArrowLeftIcon,
  CircleAlertIcon,
  FileQuestionIcon,
  FileSearchIcon,
  Share2Icon,
  TelescopeIcon,
} from "lucide-react";
import { toast } from "sonner";

import { AnalysisStatusChip } from "@/components/analysis/VerdictBadge";
import {
  DocumentSheet,
  type DocumentSheetTarget,
} from "@/components/documents/DocumentSheet";
import { ActorsPanel } from "@/components/investigation/ActorsPanel";
import { AlternativesPanel } from "@/components/investigation/AlternativesPanel";
import { CausalChainList } from "@/components/investigation/CausalChainList";
import { buildFindingsById } from "@/components/investigation/CitationChip";
import { InvestigationTimeline } from "@/components/investigation/InvestigationTimeline";
import { OpenQuestionsPanel } from "@/components/investigation/OpenQuestionsPanel";
import {
  HypothesisBadge,
  SPECULATION_BORDER_CLASS,
} from "@/components/investigation/Speculation";
import { InvestigationStageTimeline } from "@/components/investigation/StageTimeline";
import { WatchNextPanel } from "@/components/investigation/WatchNextPanel";
import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { ShareDossierDialog } from "@/components/shared/ShareDialog";
import { OwnerByline, VisibilityBadge } from "@/components/shared/Visibility";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { InvestigationDetail, InvestigationFinding } from "@/lib/api";
import { formatUsd, relativeTime } from "@/lib/format";
import { useCancelInvestigation, useInvestigation, useMe } from "@/lib/queries";
import { cn } from "@/lib/utils";

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  );
}

/** One row of the raw findings ledger (the evidence backbone of the dossier). */
function FindingCard({
  finding,
  onOpenDocument,
}: {
  finding: InvestigationFinding;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  return (
    <li
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border bg-card px-3 py-2.5",
        finding.speculation && SPECULATION_BORDER_CLASS,
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="min-w-0 flex-1 text-sm leading-6">{finding.text}</p>
        <span className="flex shrink-0 items-center gap-1.5">
          <Badge variant="outline">{finding.kind.replace(/_/g, " ")}</Badge>
          {finding.speculation ? <HypothesisBadge /> : null}
          <span className="font-mono text-[10px] text-muted-foreground">
            f{finding.id}
          </span>
        </span>
      </div>
      {finding.evidence.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {finding.evidence.map((evidence) => (
            <Button
              key={evidence.document_id}
              variant="ghost"
              size="xs"
              className="max-w-full"
              onClick={() =>
                onOpenDocument({
                  documentId: evidence.document_id,
                  quote: evidence.quote,
                })
              }
            >
              <FileSearchIcon data-icon="inline-start" />
              <span className="truncate">
                {evidence.source_name ??
                  evidence.title ??
                  `document #${evidence.document_id}`}
              </span>
            </Button>
          ))}
        </div>
      ) : null}
    </li>
  );
}

function LiveSectionHint() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-28 w-full rounded-xl" />
      <Skeleton className="h-28 w-full rounded-xl" />
      <p className="text-center text-xs text-muted-foreground">
        Sections appear as the scope, investigate, and synthesize stages
        complete.
      </p>
    </div>
  );
}

function SectionsColumn({
  detail,
  isLive,
  onOpenDocument,
}: {
  detail: InvestigationDetail;
  isLive: boolean;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const { sections } = detail;
  const findingsById = buildFindingsById(detail.findings);
  const hasAnySection =
    sections.timeline ||
    sections.causal_narrative ||
    sections.actors ||
    sections.alternatives ||
    sections.watch_next;
  const hasAnything =
    hasAnySection || detail.questions.length > 0 || detail.findings.length > 0;

  if (!hasAnything) {
    if (isLive) return <LiveSectionHint />;
    return (
      <EmptyState
        icon={TelescopeIcon}
        title="Nothing recorded"
        description="This investigation produced no findings, questions, or sections."
      />
    );
  }

  return (
    <div className="space-y-8">
      {sections.timeline ? (
        <Section title="Timeline">
          <InvestigationTimeline
            section={sections.timeline}
            findings={detail.findings}
            onOpenDocument={onOpenDocument}
          />
        </Section>
      ) : null}

      {sections.causal_narrative ? (
        <Section title="Causal narrative">
          <CausalChainList
            section={sections.causal_narrative}
            findings={findingsById}
            onOpenDocument={onOpenDocument}
          />
        </Section>
      ) : null}

      {sections.actors ? (
        <Section title="Actors">
          <ActorsPanel
            section={sections.actors}
            findings={findingsById}
            onOpenDocument={onOpenDocument}
          />
        </Section>
      ) : null}

      {sections.alternatives ? (
        <Section title="Alternatives">
          <AlternativesPanel
            section={sections.alternatives}
            questions={detail.questions}
            findings={findingsById}
            onOpenDocument={onOpenDocument}
          />
        </Section>
      ) : null}

      <Section title="Open questions">
        <OpenQuestionsPanel
          questions={detail.questions}
          investigationId={detail.id}
        />
      </Section>

      {sections.watch_next ? (
        <Section title="Watch next">
          <WatchNextPanel
            section={sections.watch_next}
            questions={detail.questions}
          />
        </Section>
      ) : null}

      {detail.findings.length > 0 ? (
        <Section title={`Findings (${detail.findings.length})`}>
          <ul className="space-y-2">
            {detail.findings.map((finding) => (
              <FindingCard
                key={finding.id}
                finding={finding}
                onOpenDocument={onOpenDocument}
              />
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

function InvestigationSkeleton() {
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

/** One route for live and finished investigations, mirroring /analysis/[id]. */
export default function InvestigationPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const investigationId = Number(id);
  const { query, activity, isLive } = useInvestigation(investigationId);
  const cancel = useCancelInvestigation();
  const me = useMe();
  const [sheetTarget, setSheetTarget] = useState<DocumentSheetTarget | null>(
    null,
  );
  const [shareOpen, setShareOpen] = useState(false);

  // Owner-only share affordance (admins can't even read others' private work
  // — private is absolute), shown only while the dossier is still private.
  const canShare =
    query.data?.visibility === "private" &&
    query.data.owner_id != null &&
    query.data.owner_id === me.data?.id;

  if (!Number.isFinite(investigationId)) {
    return (
      <EmptyState
        icon={FileQuestionIcon}
        title="Invalid investigation id"
        description={`“${id}” is not an investigation id.`}
      />
    );
  }

  const onCancel = () =>
    cancel.mutate(investigationId, {
      onSuccess: () => toast.info("Cancellation requested"),
      onError: (error) =>
        toast.error("Could not cancel", { description: error.message }),
    });

  return (
    <>
      <Button variant="ghost" size="sm" render={<Link href="/investigations" />}>
        <ArrowLeftIcon data-icon="inline-start" />
        Investigations
      </Button>

      {query.isPending ? (
        <InvestigationSkeleton />
      ) : query.isError ? (
        <QueryError error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold tracking-tight">
                {query.data.title ?? `Investigation #${query.data.id}`}
              </h1>
              <AnalysisStatusChip status={query.data.status} />
              <Badge variant="outline">{query.data.input_type}</Badge>
              {query.data.parent_question_id != null ? (
                <Badge variant="outline" title="Spawned from an open question">
                  follow-up
                </Badge>
              ) : null}
              <VisibilityBadge visibility={query.data.visibility} showShared />
              {query.data.visibility === "shared" ? (
                <OwnerByline
                  ownerId={query.data.owner_id}
                  ownerName={query.data.owner_name}
                />
              ) : null}
              <span className="text-xs text-muted-foreground">
                started {relativeTime(query.data.created_at)}
                {query.data.finished_at
                  ? ` · finished ${relativeTime(query.data.finished_at)}`
                  : ""}
                {typeof query.data.cost_usd === "number"
                  ? ` · ${formatUsd(query.data.cost_usd)}`
                  : ""}
              </span>
              {canShare ? (
                <Button
                  variant="outline"
                  size="xs"
                  className="ml-auto"
                  onClick={() => setShareOpen(true)}
                >
                  <Share2Icon data-icon="inline-start" />
                  Share
                </Button>
              ) : null}
            </div>
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
              <InvestigationStageTimeline
                stages={query.data.stages}
                activity={activity}
                isLive={isLive}
                costUsd={query.data.cost_usd}
                budgetUsd={query.data.budget_usd}
                onCancel={onCancel}
                cancelling={cancel.isPending}
              />
            </aside>
            <div className="min-w-0 flex-1">
              <SectionsColumn
                detail={query.data}
                isLive={isLive}
                onOpenDocument={setSheetTarget}
              />
            </div>
          </div>
        </>
      )}

      <ShareDossierDialog
        kind="investigation"
        id={investigationId}
        open={shareOpen}
        onOpenChange={setShareOpen}
      />

      <DocumentSheet
        target={sheetTarget}
        onOpenChange={(open) => {
          if (!open) setSheetTarget(null);
        }}
      />
    </>
  );
}
