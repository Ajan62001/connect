"use client";

import { useState } from "react";
import { FileSearchIcon } from "lucide-react";

import { StanceChip } from "@/components/analysis/VerdictBadge";
import type { DocumentSheetTarget } from "@/components/documents/DocumentSheet";
import { TierDots } from "@/components/sources/TierDots";
import { Button } from "@/components/ui/button";
import type { AnalysisEvidence, CredibilityTier } from "@/lib/api";
import { urlHost } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * One evidence document judged against a claim: source + tier, stance chip,
 * the verbatim quote (clamped to 3 lines, expandable), and "open document"
 * which opens the DocumentSheet scrolled to that quote.
 */
export function StanceCard({
  evidence,
  onOpenDocument,
}: {
  evidence: AnalysisEvidence;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const tier =
    evidence.credibility_tier !== null &&
    evidence.credibility_tier >= 1 &&
    evidence.credibility_tier <= 4
      ? (evidence.credibility_tier as CredibilityTier)
      : null;

  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-card p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {evidence.source_name ??
            (evidence.url ? urlHost(evidence.url) : "Unknown source")}
        </span>
        {tier !== null ? <TierDots tier={tier} /> : null}
        <StanceChip stance={evidence.stance} />
      </div>
      {evidence.title ? (
        <p className="truncate text-xs text-muted-foreground" title={evidence.title}>
          {evidence.title}
        </p>
      ) : null}
      <blockquote
        className={cn(
          "border-l-2 border-border pl-3 text-sm leading-6 text-muted-foreground",
          !expanded && "line-clamp-3",
        )}
      >
        {evidence.quote}
      </blockquote>
      <div className="mt-auto flex items-center justify-between gap-2">
        <Button
          variant="ghost"
          size="xs"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? "Show less" : "Show more"}
        </Button>
        <Button
          variant="outline"
          size="xs"
          onClick={() =>
            onOpenDocument({
              documentId: evidence.document_id,
              quote: evidence.quote,
            })
          }
        >
          <FileSearchIcon data-icon="inline-start" />
          Open document
        </Button>
      </div>
    </div>
  );
}
