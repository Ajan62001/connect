"use client";

import {
  BanIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleSlashIcon,
  CircleXIcon,
  Loader2Icon,
} from "lucide-react";

import { ActivityLog } from "@/components/analysis/ActivityLog";
import { Button } from "@/components/ui/button";
import type { AnalysisStageName, AnalysisStageRun, AnalysisStatus } from "@/lib/api";
import { ANALYSIS_STAGES } from "@/lib/api";
import { parseTimestamp } from "@/lib/format";
import type { AnalysisActivityLine } from "@/lib/queries";
import { cn } from "@/lib/utils";

const STAGE_LABELS: Record<AnalysisStageName, string> = {
  normalize: "Normalize",
  verify: "Verify",
  assemble: "Assemble",
};

const STAGE_HINTS: Record<AnalysisStageName, string> = {
  normalize: "Decompose the input into checkable claims",
  verify: "Gather evidence and judge stances per claim",
  assemble: "Aggregate verdicts and write reasoning",
};

function StageIcon({ status }: { status: AnalysisStatus }) {
  switch (status) {
    case "running":
      return <Loader2Icon className="size-4 animate-spin text-blue-600 dark:text-blue-400" aria-hidden />;
    case "completed":
      return <CircleCheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" aria-hidden />;
    case "failed":
      return <CircleXIcon className="size-4 text-destructive" aria-hidden />;
    case "cancelled":
      return <CircleSlashIcon className="size-4 text-muted-foreground" aria-hidden />;
    case "pending":
      return <CircleDashedIcon className="size-4 text-muted-foreground/60" aria-hidden />;
  }
}

/** "4s" / "1m 12s" between two backend timestamps; null while incomplete. */
function stageDuration(run: AnalysisStageRun): string | null {
  if (!run.started_at || !run.finished_at) return null;
  const start = parseTimestamp(run.started_at);
  const end = parseTimestamp(run.finished_at);
  if (!start || !end) return null;
  const seconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/**
 * Left-rail pipeline timeline: normalize -> verify -> assemble. Stages the
 * backend hasn't reported yet render as pending; the running stage hosts the
 * ActivityLog tail. Cancel is offered while the analysis is in flight.
 */
export function StageTimeline({
  stages,
  activity,
  isLive,
  onCancel,
  cancelling,
}: {
  stages: AnalysisStageRun[];
  activity: AnalysisActivityLine[];
  isLive: boolean;
  onCancel: () => void;
  cancelling: boolean;
}) {
  // Snapshot order is authoritative when present; missing stages are pending.
  const runs: AnalysisStageRun[] = ANALYSIS_STAGES.map(
    (name) =>
      stages.find((s) => s.stage === name) ?? {
        stage: name,
        status: "pending" as const,
        summary: null,
        started_at: null,
        finished_at: null,
      },
  );

  return (
    <div className="rounded-xl border bg-card p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Pipeline
      </p>
      <ol className="mt-3">
        {runs.map((run, i) => {
          const duration = stageDuration(run);
          const last = i === runs.length - 1;
          return (
            <li key={run.stage} className="relative flex gap-3 pb-1">
              {!last ? (
                <span
                  aria-hidden
                  className="absolute top-5 left-2 h-[calc(100%-1.25rem)] w-px -translate-x-1/2 bg-border"
                />
              ) : null}
              <span className="z-10 mt-0.5 flex size-4 shrink-0 items-center justify-center bg-card">
                <StageIcon status={run.status} />
              </span>
              <div className={cn("min-w-0 flex-1", last ? "" : "pb-4")}>
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span
                    className={cn(
                      "text-sm font-medium",
                      run.status === "pending" && "text-muted-foreground",
                    )}
                  >
                    {STAGE_LABELS[run.stage]}
                  </span>
                  {duration ? (
                    <span className="text-xs text-muted-foreground">{duration}</span>
                  ) : null}
                </div>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {run.summary ?? STAGE_HINTS[run.stage]}
                </p>
                {run.status === "running" ? <ActivityLog lines={activity} /> : null}
              </div>
            </li>
          );
        })}
      </ol>
      {isLive ? (
        <Button
          variant="outline"
          size="sm"
          className="mt-3 w-full"
          disabled={cancelling}
          onClick={onCancel}
        >
          {cancelling ? (
            <Loader2Icon className="animate-spin" data-icon="inline-start" />
          ) : (
            <BanIcon data-icon="inline-start" />
          )}
          Cancel analysis
        </Button>
      ) : null}
    </div>
  );
}
