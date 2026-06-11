"use client";

import Link from "next/link";
import { CornerDownRightIcon } from "lucide-react";

import type { DocumentSheetTarget } from "@/components/documents/DocumentSheet";
import {
  CitationChip,
  type FindingsById,
} from "@/components/investigation/CitationChip";
import { NarrativeMarkdown } from "@/components/investigation/NarrativeMarkdown";
import {
  HypothesisBadge,
  SPECULATION_BORDER_CLASS,
} from "@/components/investigation/Speculation";
import type { CausalNarrativeSection, ChainStep } from "@/lib/api";
import { cn } from "@/lib/utils";

function nodeHref(step: ChainStep): string | null {
  switch (step.node_type) {
    case "event":
      return `/event/${step.node_id}`;
    case "entity":
      return `/entity/${step.node_id}`;
    case "document":
      return `/documents/${step.node_id}`;
    default:
      return null;
  }
}

const STEP_INDENT_PX = 20;

function ChainStepRow({
  step,
  index,
  findings,
  onOpenDocument,
}: {
  step: ChainStep;
  index: number;
  findings: FindingsById;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const href = nodeHref(step);
  return (
    <li style={{ marginLeft: index * STEP_INDENT_PX }}>
      <div
        className={cn(
          "inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border bg-card px-2.5 py-1.5 text-sm",
          step.speculation ? SPECULATION_BORDER_CLASS : "border-border",
        )}
      >
        {href ? (
          <Link
            href={href}
            className="min-w-0 truncate font-medium underline-offset-4 hover:underline"
          >
            {step.title}
          </Link>
        ) : (
          <span className="min-w-0 truncate font-medium">{step.title}</span>
        )}
        {step.speculation ? <HypothesisBadge /> : null}
        {typeof step.finding_id === "number" ? (
          <CitationChip
            findingId={step.finding_id}
            findings={findings}
            onOpenDocument={onOpenDocument}
          />
        ) : null}
      </div>
      {step.relation_to_next ? (
        <p className="mt-1 mb-1 flex items-center gap-1 pl-3 text-xs text-muted-foreground">
          <CornerDownRightIcon className="size-3.5 shrink-0" aria-hidden />
          {step.relation_to_next.replace(/_/g, " ")}
        </p>
      ) : null}
    </li>
  );
}

/**
 * The causal_narrative section: the DEEP narrative (with [[f#]] citation
 * chips) followed by each causal chain as an indented step list joined by
 * relation connectors. Steps citing speculative findings inherit the shared
 * dashed hypothesis treatment.
 */
export function CausalChainList({
  section,
  findings,
  onOpenDocument,
}: {
  section: CausalNarrativeSection;
  findings: FindingsById;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const chains = section.chains ?? [];
  return (
    <div className="space-y-5">
      {section.narrative_md ? (
        <NarrativeMarkdown
          markdown={section.narrative_md}
          findings={findings}
          onOpenDocument={onOpenDocument}
        />
      ) : null}
      {chains.length > 0 ? (
        <div className="space-y-4">
          {chains.map((chain, c) => (
            <ol key={c} className="space-y-0.5 overflow-x-auto rounded-xl border bg-muted/20 p-3">
              {(chain.steps ?? []).map((step, s) => (
                <ChainStepRow
                  key={`${step.node_type}-${step.node_id}-${s}`}
                  step={step}
                  index={s}
                  findings={findings}
                  onOpenDocument={onOpenDocument}
                />
              ))}
            </ol>
          ))}
        </div>
      ) : null}
    </div>
  );
}
