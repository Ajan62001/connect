"use client";

import { useEffect, useRef } from "react";

import { Skeleton } from "@/components/ui/skeleton";

/**
 * Minimum a log line needs. Both AnalysisActivityLine and
 * InvestigationActivityLine satisfy this structurally.
 */
export interface ActivityLogLine {
  seq: number;
  message: string;
}

/**
 * The progress tail rendered inside the currently running stage of a
 * StageTimeline (analysis stage_progress lines, investigation iteration
 * briefs). Lines come from the live hook's in-hook ring buffer (last 200,
 * never the query cache); shows a skeleton shimmer until the first line lands.
 */
export function ActivityLog({
  lines,
  maxLines = 8,
}: {
  lines: ActivityLogLine[];
  maxLines?: number;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [lines.length]);

  if (lines.length === 0) {
    return (
      <div className="mt-2 space-y-1.5" aria-label="Waiting for progress">
        <Skeleton className="h-3 w-5/6" />
        <Skeleton className="h-3 w-2/3" />
        <Skeleton className="h-3 w-3/4" />
      </div>
    );
  }

  const tail = lines.slice(-maxLines);
  return (
    <div
      className="mt-2 max-h-40 space-y-1 overflow-y-auto rounded-md bg-muted/50 p-2"
      aria-live="polite"
    >
      {tail.map((line, i) => (
        <p
          key={line.seq > 0 ? line.seq : `i${i}`}
          className="font-mono text-[11px] leading-4 text-muted-foreground"
        >
          {line.message}
        </p>
      ))}
      <div ref={endRef} />
    </div>
  );
}
