"use client";

import { Fragment, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  FlaskConicalIcon,
  Loader2Icon,
  ScaleIcon,
} from "lucide-react";
import { toast } from "sonner";

import { VerdictBadge } from "@/components/analysis/VerdictBadge";
import {
  DocumentSheet,
  type DocumentSheetTarget,
} from "@/components/documents/DocumentSheet";
import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { Paginator } from "@/components/shared/Paginator";
import { QueryError } from "@/components/shared/QueryError";
import { TierDots } from "@/components/sources/TierDots";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api";
import type {
  AnalysisEvidence,
  ContradictionListItem,
  ContradictionStatus,
  CredibilityTier,
} from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  useContradiction,
  useContradictions,
  useCreateAnalysis,
  useDismissContradiction,
} from "@/lib/queries";

const PAGE_SIZE = 20;

const STATUS_TABS: { value: ContradictionStatus; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "dismissed", label: "Dismissed" },
  { value: "resolved", label: "Resolved" },
];

function tierOrNull(tier: number): CredibilityTier | null {
  return tier >= 1 && tier <= 4 ? (tier as CredibilityTier) : null;
}

function SideCount({ count, tier }: { count: number; tier: number }) {
  const validTier = tierOrNull(tier);
  return (
    <span className="inline-flex items-center gap-2">
      <span className="tabular-nums">{count}</span>
      {count > 0 && validTier !== null ? <TierDots tier={validTier} /> : null}
    </span>
  );
}

function QuoteList({
  title,
  count,
  evidence,
  accentClass,
  onOpenDocument,
}: {
  title: string;
  count: number;
  evidence: AnalysisEvidence[];
  accentClass: string;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  return (
    <div className="min-w-0 space-y-2">
      <p className={`text-xs font-medium uppercase tracking-wide ${accentClass}`}>
        {title} ({count})
      </p>
      {evidence.length === 0 ? (
        <p className="text-xs text-muted-foreground">No quotes available.</p>
      ) : (
        <ul className="space-y-2">
          {evidence.map((item) => {
            const tier = item.credibility_tier !== null ? tierOrNull(item.credibility_tier) : null;
            return (
              <li key={item.id} className="rounded-lg border bg-card p-2.5">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="min-w-0 flex-1 truncate text-xs font-medium">
                    {item.source_name ?? "Unknown source"}
                  </span>
                  {tier !== null ? <TierDots tier={tier} /> : null}
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={() =>
                      onOpenDocument({
                        documentId: item.document_id,
                        quote: item.quote,
                      })
                    }
                  >
                    open
                  </Button>
                </div>
                <blockquote className="mt-1 border-l-2 border-border pl-2 text-xs leading-5 text-muted-foreground">
                  {item.quote}
                </blockquote>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Expanded row body: the two-column support/refute quote lists. */
function ContradictionEvidence({
  row,
  onOpenDocument,
}: {
  row: ContradictionListItem;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const detail = useContradiction(row.id);

  if (detail.isPending) {
    return (
      <div className="grid gap-4 sm:grid-cols-2">
        <Skeleton className="h-20 w-full rounded-lg" />
        <Skeleton className="h-20 w-full rounded-lg" />
      </div>
    );
  }
  if (detail.isError) {
    return (
      <p className="text-xs text-muted-foreground">
        Evidence quotes aren&apos;t available ({detail.error.message}) —
        {` ${row.n_support} supporting vs ${row.n_refute} refuting sightings.`}
      </p>
    );
  }

  const evidence = detail.data.evidence ?? [];
  const support = evidence.filter((e) => e.stance === "supports");
  const refute = evidence.filter((e) => e.stance === "refutes");

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <QuoteList
        title="Supports"
        count={row.n_support}
        evidence={support}
        accentClass="text-emerald-700 dark:text-emerald-400"
        onOpenDocument={onOpenDocument}
      />
      <QuoteList
        title="Refutes"
        count={row.n_refute}
        evidence={refute}
        accentClass="text-red-700 dark:text-red-400"
        onOpenDocument={onOpenDocument}
      />
    </div>
  );
}

export default function ContradictionsPage() {
  const router = useRouter();
  const [status, setStatus] = useState<ContradictionStatus>("open");
  const [page, setPage] = useState(1);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [sheetTarget, setSheetTarget] = useState<DocumentSheetTarget | null>(null);

  const contradictions = useContradictions({ status, page, page_size: PAGE_SIZE });
  const dismiss = useDismissContradiction();
  const createAnalysis = useCreateAnalysis();

  const onDismiss = (id: number) =>
    dismiss.mutate(id, {
      onSuccess: () => toast.success("Contradiction dismissed"),
      onError: (error) =>
        toast.error("Could not dismiss", { description: error.message }),
    });

  const onAnalyze = (row: ContradictionListItem) =>
    createAnalysis.mutate(
      { input_text: row.claim.text },
      {
        onSuccess: (accepted) => router.push(`/analysis/${accepted.analysis_id}`),
        onError: (error) =>
          toast.error("Could not start analysis", { description: error.message }),
      },
    );

  return (
    <>
      <PageHeader
        title="Contradictions"
        description="Claims your sources disagree on, materialized by the evidence scan."
      />

      <Tabs
        value={status}
        onValueChange={(value) => {
          setStatus(value as ContradictionStatus);
          setPage(1);
          setExpandedId(null);
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

      {contradictions.isPending ? (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full rounded-lg" />
          ))}
        </div>
      ) : contradictions.isError ? (
        contradictions.error instanceof ApiError &&
        contradictions.error.status === 404 ? (
          <EmptyState
            icon={ScaleIcon}
            title="Contradictions API not available yet"
            description="The backend hasn't shipped the verification endpoints; this ledger will light up once it does."
          />
        ) : (
          <QueryError
            error={contradictions.error}
            onRetry={() => void contradictions.refetch()}
          />
        )
      ) : contradictions.data.items.length === 0 ? (
        <EmptyState
          icon={ScaleIcon}
          title={`No ${status} contradictions`}
          description={
            status === "open"
              ? "Nothing your sources currently disagree on — the scan runs after evidence lands."
              : `Nothing has been ${status} yet.`
          }
        />
      ) : (
        <>
          <div className="overflow-hidden rounded-xl border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>Claim</TableHead>
                  <TableHead className="whitespace-nowrap">Support</TableHead>
                  <TableHead className="whitespace-nowrap">Refute</TableHead>
                  <TableHead className="whitespace-nowrap">Detected</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {contradictions.data.items.map((row) => {
                  const expanded = expandedId === row.id;
                  return (
                    <Fragment key={row.id}>
                      <TableRow
                        className="cursor-pointer"
                        onClick={() => setExpandedId(expanded ? null : row.id)}
                      >
                        <TableCell className="pr-0">
                          {expanded ? (
                            <ChevronDownIcon
                              className="size-4 text-muted-foreground"
                              aria-hidden
                            />
                          ) : (
                            <ChevronRightIcon
                              className="size-4 text-muted-foreground"
                              aria-hidden
                            />
                          )}
                        </TableCell>
                        <TableCell className="max-w-md">
                          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            <span className="line-clamp-2 min-w-0 text-sm">
                              {row.claim.text}
                            </span>
                            <VerdictBadge verdict={row.claim.verdict} />
                          </span>
                        </TableCell>
                        <TableCell>
                          <SideCount count={row.n_support} tier={row.best_tier_support} />
                        </TableCell>
                        <TableCell>
                          <SideCount count={row.n_refute} tier={row.best_tier_refute} />
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                          {relativeTime(row.detected_at)}
                        </TableCell>
                        <TableCell
                          className="text-right"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <span className="inline-flex items-center gap-1.5">
                            {row.status === "open" ? (
                              <Button
                                variant="ghost"
                                size="xs"
                                disabled={
                                  dismiss.isPending && dismiss.variables === row.id
                                }
                                onClick={() => onDismiss(row.id)}
                              >
                                {dismiss.isPending && dismiss.variables === row.id ? (
                                  <Loader2Icon
                                    className="animate-spin"
                                    data-icon="inline-start"
                                  />
                                ) : null}
                                Dismiss
                              </Button>
                            ) : null}
                            <Button
                              variant="outline"
                              size="xs"
                              disabled={createAnalysis.isPending}
                              onClick={() => onAnalyze(row)}
                            >
                              <FlaskConicalIcon data-icon="inline-start" />
                              Analyze
                            </Button>
                          </span>
                        </TableCell>
                      </TableRow>
                      {expanded ? (
                        <TableRow className="hover:bg-transparent">
                          <TableCell colSpan={6} className="bg-muted/30 p-4">
                            <ContradictionEvidence
                              row={row}
                              onOpenDocument={setSheetTarget}
                            />
                          </TableCell>
                        </TableRow>
                      ) : null}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <Paginator
            page={contradictions.data.page}
            pageSize={contradictions.data.page_size}
            total={contradictions.data.total}
            onPageChange={setPage}
          />
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
