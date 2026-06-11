"use client";

import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import type { EvolutionSummary } from "@/lib/api";
import { relativeTime } from "@/lib/format";

/** [[s12]] citation markers emitted by the summary call (statement ids). */
const MARKER_RE = /\[\[s(\d+)\]\]/g;

/**
 * One [[s<id>]] marker as a small chip. Ids present in the statement list are
 * clickable and scroll to the matching PositionCard; unknown ids (statement
 * fell out of the list) render as inert muted chips.
 */
function CitationMarker({
  statementId,
  known,
  onCite,
}: {
  statementId: number;
  known: boolean;
  onCite: (statementId: number) => void;
}) {
  const className =
    "mx-0.5 inline-flex h-4 shrink-0 items-center rounded-md border border-border px-1 align-text-top font-mono text-[10px] leading-none text-muted-foreground";
  if (!known) {
    return (
      <span
        className={`${className} opacity-60`}
        title="Statement not in this list"
      >
        s{statementId}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={`${className} cursor-pointer transition-colors hover:bg-accent hover:text-accent-foreground`}
      title="Jump to statement"
      onClick={() => onCite(statementId)}
    >
      s{statementId}
    </button>
  );
}

function renderWithMarkers(
  text: string,
  keyPrefix: string,
  knownIds: ReadonlySet<number>,
  onCite: (statementId: number) => void,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(MARKER_RE)) {
    const index = match.index ?? 0;
    if (index > last) nodes.push(text.slice(last, index));
    const statementId = Number(match[1]);
    nodes.push(
      <CitationMarker
        key={`${keyPrefix}-s${i++}`}
        statementId={statementId}
        known={knownIds.has(statementId)}
        onCite={onCite}
      />,
    );
    last = index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

/**
 * The cached "how the position evolved" summary at the top of a topic panel.
 * Citations render as small markers linking to the statement cards below;
 * `stale: true` (regeneration blocked by the budget governor) shows an amber
 * indicator instead of failing the read.
 */
export function EvolutionSummaryCard({
  summary,
  knownIds,
  onCite,
}: {
  summary: EvolutionSummary;
  knownIds: ReadonlySet<number>;
  onCite: (statementId: number) => void;
}) {
  const paragraphs = summary.text
    .split(/\n[ \t]*\n+/)
    .map((block) => block.trim())
    .filter(Boolean);

  return (
    <div className="space-y-2 rounded-lg border bg-muted/30 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          How the position evolved
        </p>
        {summary.stale ? (
          <Badge
            variant="outline"
            className="border-amber-300 text-amber-700 dark:border-amber-800 dark:text-amber-400"
            title="The summary lags the newest statements — it refreshes on a later read once budget allows."
          >
            stale
          </Badge>
        ) : null}
        <span className="ml-auto text-xs text-muted-foreground">
          generated {relativeTime(summary.generated_at)}
        </span>
      </div>
      <div className="max-w-prose space-y-2 text-sm leading-6">
        {paragraphs.map((paragraph, p) => (
          <p key={p}>{renderWithMarkers(paragraph, `p${p}`, knownIds, onCite)}</p>
        ))}
      </div>
    </div>
  );
}
