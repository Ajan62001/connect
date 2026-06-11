"use client";

import Link from "next/link";

import type { DocumentSheetTarget } from "@/components/documents/DocumentSheet";
import { EventTypeChip } from "@/components/events/EventTimeline";
import { openFindingEvidence } from "@/components/investigation/CitationChip";
import {
  HypothesisBadge,
  SPECULATION_BORDER_CLASS,
} from "@/components/investigation/Speculation";
import type {
  InvestigationFinding,
  TimelineCausalChip,
  TimelineSection,
  TimelineSectionItem,
} from "@/lib/api";
import { formatDay } from "@/lib/format";
import { cn } from "@/lib/utils";

/** "reaction_to" -> "reaction to" for chip labels. */
function relationLabel(relation: string): string {
  return relation.replace(/_/g, " ");
}

/**
 * One causal connection under a timeline item. Solid border = quote-grounded
 * edge; dashed + hypothesis badge = speculation. Clicking opens the backing
 * finding's evidence quote (edge_id -> finding.edge_id join); edges without a
 * local finding (e.g. surfaced from a prior investigation) render inert.
 */
function CausalChipButton({
  chip,
  findingsByEdge,
  onOpenDocument,
}: {
  chip: TimelineCausalChip;
  findingsByEdge: ReadonlyMap<number, InvestigationFinding>;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const finding = findingsByEdge.get(chip.edge_id);
  const clickable = (finding?.evidence.length ?? 0) > 0;
  const className = cn(
    "inline-flex max-w-full items-center gap-1.5 rounded-md border bg-card px-2 py-0.5 text-xs",
    chip.speculation ? SPECULATION_BORDER_CLASS : "border-border",
    clickable
      ? "cursor-pointer transition-colors hover:bg-accent hover:text-accent-foreground"
      : "cursor-default",
  );
  const body = (
    <>
      <span className="shrink-0 font-medium text-muted-foreground">
        {relationLabel(chip.relation)}
      </span>
      <span className="truncate">
        {chip.other_title ?? `event #${chip.other_id}`}
      </span>
      {chip.speculation ? <HypothesisBadge /> : null}
    </>
  );

  if (!clickable) {
    return (
      <span className={className} title={finding?.text}>
        {body}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={className}
      title={finding?.text ?? "Open the grounding quote"}
      onClick={() => openFindingEvidence(finding, onOpenDocument)}
    >
      {body}
    </button>
  );
}

function TimelineRow({
  item,
  findingsByEdge,
  onOpenDocument,
}: {
  item: TimelineSectionItem;
  findingsByEdge: ReadonlyMap<number, InvestigationFinding>;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const href = item.event_id
    ? `/event/${item.event_id}`
    : item.document_id
      ? `/documents/${item.document_id}`
      : null;

  return (
    <li className="relative pb-6 pl-6 last:pb-0">
      <span
        className="absolute top-1 left-0 size-2.5 -translate-x-1/2 rounded-full border-2 border-background bg-primary/70"
        aria-hidden
      />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-xs text-muted-foreground tabular-nums">
          {item.date ? formatDay(item.date) : "undated"}
        </span>
        {item.event_type ? <EventTypeChip type={item.event_type} /> : null}
        {typeof item.doc_count === "number" && item.doc_count > 0 ? (
          <span className="text-xs text-muted-foreground">
            {item.doc_count} doc{item.doc_count === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>
      {href ? (
        <Link
          href={href}
          className="mt-1 block text-sm font-medium underline-offset-4 hover:underline"
        >
          {item.title}
        </Link>
      ) : (
        <p className="mt-1 text-sm font-medium">{item.title}</p>
      )}
      {item.causal.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {item.causal.map((chip) => (
            <CausalChipButton
              key={chip.edge_id}
              chip={chip}
              findingsByEdge={findingsByEdge}
              onOpenDocument={onOpenDocument}
            />
          ))}
        </div>
      ) : null}
    </li>
  );
}

/**
 * The investigation's vertical timeline section: events + undated docs in
 * date order, each with its causal connections rendered as chips underneath.
 */
export function InvestigationTimeline({
  section,
  findings,
  onOpenDocument,
}: {
  section: TimelineSection;
  findings: InvestigationFinding[];
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const findingsByEdge = new Map(
    findings
      .filter((finding) => finding.edge_id !== null)
      .map((finding) => [finding.edge_id as number, finding]),
  );
  const items = section.items ?? [];
  if (items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        The scope pass found nothing to place on a timeline.
      </p>
    );
  }
  return (
    <ol className="ml-1.5 border-l">
      {items.map((item, i) => (
        <TimelineRow
          key={`${item.event_id ?? "d"}-${item.document_id ?? "e"}-${i}`}
          item={item}
          findingsByEdge={findingsByEdge}
          onOpenDocument={onOpenDocument}
        />
      ))}
    </ol>
  );
}
