"use client";

import Link from "next/link";

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
import { Badge } from "@/components/ui/badge";
import type { ActorsSection } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Who acted and why: one card per actor with role, motive narrative (with
 * [[f#]] citations), and the backing finding chips. Speculative motive
 * readings get the shared dashed hypothesis treatment.
 */
export function ActorsPanel({
  section,
  findings,
  onOpenDocument,
}: {
  section: ActorsSection;
  findings: FindingsById;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const actors = section.actors ?? [];
  if (actors.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No actors surfaced in this investigation.
      </p>
    );
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {actors.map((actor, i) => (
        <div
          key={actor.entity_id ?? `a${i}`}
          className={cn(
            "flex flex-col gap-2 rounded-xl border bg-card p-4",
            actor.speculation && SPECULATION_BORDER_CLASS,
          )}
        >
          <div className="flex flex-wrap items-center gap-2">
            {actor.entity_id !== null ? (
              <Link
                href={`/entity/${actor.entity_id}`}
                className="text-sm font-semibold underline-offset-4 hover:underline"
              >
                {actor.name}
              </Link>
            ) : (
              <span className="text-sm font-semibold">{actor.name}</span>
            )}
            {actor.role ? <Badge variant="outline">{actor.role}</Badge> : null}
            {actor.speculation ? <HypothesisBadge /> : null}
          </div>
          {actor.motive_md ? (
            <NarrativeMarkdown
              markdown={actor.motive_md}
              findings={findings}
              onOpenDocument={onOpenDocument}
              className="text-muted-foreground [&_p]:leading-6"
            />
          ) : null}
          {(actor.finding_ids ?? []).length > 0 ? (
            <div className="mt-auto flex flex-wrap items-center gap-1 pt-1">
              {actor.finding_ids.map((findingId) => (
                <CitationChip
                  key={findingId}
                  findingId={findingId}
                  findings={findings}
                  onOpenDocument={onOpenDocument}
                />
              ))}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
