import { Loader2Icon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type {
  AnalysisStatus,
  ClaimVerdict,
  EvidenceStance,
  VerdictSummary,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Shared palette: supported/supports = green, refuted/refutes = red,
// mixed = amber, unverified/unrelated = gray.
// ---------------------------------------------------------------------------

const GREEN =
  "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300";
const RED = "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300";
const AMBER = "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300";
const GRAY = "bg-muted text-muted-foreground";

const VERDICT_STYLES: Record<ClaimVerdict, string> = {
  supported: GREEN,
  refuted: RED,
  mixed: AMBER,
  unverified: GRAY,
};

/**
 * Claim verdict chip, optionally with the aggregation confidence.
 * `verdict === null` (still verifying / uncheckable) renders nothing —
 * callers decide what a pending claim looks like.
 */
export function VerdictBadge({
  verdict,
  confidence,
  className,
}: {
  verdict: ClaimVerdict | null;
  confidence?: number | null;
  className?: string;
}) {
  if (!verdict) return null;
  const style = VERDICT_STYLES[verdict] ?? GRAY;
  return (
    <Badge variant="secondary" className={cn(style, className)}>
      {verdict}
      {typeof confidence === "number" && Number.isFinite(confidence) ? (
        <span className="opacity-75">{Math.round(confidence * 100)}%</span>
      ) : null}
    </Badge>
  );
}

/** Per-evidence stance chip on StanceCards. */
export function StanceChip({
  stance,
  className,
}: {
  stance: EvidenceStance;
  className?: string;
}) {
  const styles: Record<EvidenceStance, string> = {
    supports: GREEN,
    refutes: RED,
    mixed: AMBER,
    unrelated: GRAY,
  };
  return (
    <Badge variant="secondary" className={cn(styles[stance] ?? GRAY, className)}>
      {stance}
    </Badge>
  );
}

/**
 * Compact verdict counts for an analysis list row ("2 supported · 1 refuted").
 * Zero-count buckets are skipped; an all-zero summary renders nothing.
 */
export function VerdictSummaryChips({ summary }: { summary: VerdictSummary }) {
  const buckets: { key: ClaimVerdict; count: number }[] = [
    { key: "supported", count: summary.supported },
    { key: "refuted", count: summary.refuted },
    { key: "mixed", count: summary.mixed },
    { key: "unverified", count: summary.unverified },
  ];
  const visible = buckets.filter((b) => b.count > 0);
  if (visible.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {visible.map(({ key, count }) => (
        <Badge key={key} variant="secondary" className={VERDICT_STYLES[key]}>
          {count} {key}
        </Badge>
      ))}
    </span>
  );
}

const STATUS_STYLES: Record<AnalysisStatus, string> = {
  pending: GRAY,
  running: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  completed: GREEN,
  failed: "bg-destructive/10 text-destructive dark:bg-destructive/20",
  cancelled: "border-border bg-transparent text-muted-foreground",
};

/** Analysis lifecycle chip; `running` gets a spinner. */
export function AnalysisStatusChip({
  status,
  className,
}: {
  status: AnalysisStatus;
  className?: string;
}) {
  return (
    <Badge
      variant="secondary"
      className={cn(STATUS_STYLES[status] ?? GRAY, className)}
    >
      {status === "running" ? (
        <Loader2Icon className="animate-spin" aria-hidden />
      ) : null}
      {status}
    </Badge>
  );
}
