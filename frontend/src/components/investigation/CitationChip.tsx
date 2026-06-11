"use client";

import type { DocumentSheetTarget } from "@/components/documents/DocumentSheet";
import { SPECULATION_BORDER_CLASS } from "@/components/investigation/Speculation";
import type { InvestigationFinding } from "@/lib/api";
import { cn } from "@/lib/utils";

/** Findings keyed by id — built once per page from the snapshot. */
export type FindingsById = ReadonlyMap<number, InvestigationFinding>;

export function buildFindingsById(
  findings: InvestigationFinding[],
): FindingsById {
  return new Map(findings.map((finding) => [finding.id, finding]));
}

/**
 * Opens a finding's evidence: the DocumentSheet scrolled to the first
 * verbatim quote. Returns false when the finding has no evidence to show
 * (pure speculation findings have an empty evidence list by design).
 */
export function openFindingEvidence(
  finding: InvestigationFinding | undefined,
  onOpenDocument: (target: DocumentSheetTarget) => void,
): boolean {
  const evidence = finding?.evidence[0];
  if (!evidence) return false;
  onOpenDocument({ documentId: evidence.document_id, quote: evidence.quote });
  return true;
}

/**
 * An [[f#]] citation marker rendered as a clickable chip. Clicking opens the
 * finding's evidence quote in the DocumentSheet. Unknown finding ids (stripped
 * or flagged by the synthesis grounding check) render as inert muted chips;
 * speculative findings get the shared dashed hypothesis treatment.
 */
export function CitationChip({
  findingId,
  findings,
  onOpenDocument,
}: {
  findingId: number;
  findings: FindingsById;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const finding = findings.get(findingId);
  const clickable = (finding?.evidence.length ?? 0) > 0;

  const className = cn(
    "mx-0.5 inline-flex h-4 shrink-0 items-center rounded-md border px-1 align-text-top font-mono text-[10px] leading-none",
    finding?.speculation
      ? cn(SPECULATION_BORDER_CLASS, "text-amber-700 dark:text-amber-400")
      : "border-border text-muted-foreground",
    clickable
      ? "cursor-pointer transition-colors hover:bg-accent hover:text-accent-foreground"
      : "opacity-60",
  );
  const label = `f${findingId}`;

  if (!clickable) {
    return (
      <span
        className={className}
        title={finding ? finding.text : "Finding not found in this dossier"}
      >
        {label}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={className}
      title={finding?.text}
      onClick={() => openFindingEvidence(finding, onOpenDocument)}
    >
      {label}
    </button>
  );
}
