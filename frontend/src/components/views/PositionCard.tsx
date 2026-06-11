"use client";

import Link from "next/link";
import { FileTextIcon } from "lucide-react";

import { TierDots } from "@/components/sources/TierDots";
import type { CredibilityTier, Statement } from "@/lib/api";
import { absoluteTime, formatDay } from "@/lib/format";
import { cn } from "@/lib/utils";

function tierOrNull(tier: number | null): CredibilityTier | null {
  return tier !== null && tier >= 1 && tier <= 4
    ? (tier as CredibilityTier)
    : null;
}

/** DOM id for a statement card — evolution-summary citations scroll to it. */
export function statementDomId(statementId: number): string {
  return `statement-${statementId}`;
}

/**
 * One statement on the per-topic timeline: date, verbatim quote, the neutral
 * stance paraphrase, and the source line (name + tier + document link).
 * `highlighted` is the transient ring applied when a citation marker in the
 * evolution summary jumps here.
 */
export function PositionCard({
  statement,
  highlighted = false,
}: {
  statement: Statement;
  highlighted?: boolean;
}) {
  const tier = tierOrNull(statement.credibility_tier);

  return (
    <div
      id={statementDomId(statement.id)}
      className={cn(
        "scroll-mt-24 rounded-lg border bg-card p-3 transition-shadow",
        highlighted && "ring-2 ring-primary/60",
      )}
    >
      {statement.stated_at ? (
        <p
          className="text-xs font-medium text-muted-foreground"
          title={absoluteTime(statement.stated_at)}
        >
          {formatDay(statement.stated_at)}
        </p>
      ) : null}

      <blockquote className="mt-1 border-l-2 border-border pl-3 text-sm leading-6">
        &ldquo;{statement.quote}&rdquo;
      </blockquote>

      {statement.position_summary ? (
        <p className="mt-1.5 text-xs text-muted-foreground italic">
          {statement.position_summary}
        </p>
      ) : null}

      <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        {statement.source_name ? (
          <span className="font-medium text-foreground">
            {statement.source_name}
          </span>
        ) : null}
        {tier !== null ? <TierDots tier={tier} /> : null}
        <Link
          href={`/documents/${statement.document_id}`}
          className="inline-flex min-w-0 items-center gap-1 text-primary underline-offset-4 hover:underline"
        >
          <FileTextIcon className="size-3 shrink-0" aria-hidden />
          <span className="truncate">
            {statement.title ?? `Document #${statement.document_id}`}
          </span>
        </Link>
      </p>
    </div>
  );
}
