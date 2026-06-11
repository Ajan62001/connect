"use client";

import Link from "next/link";

import type { DocumentSheetTarget } from "@/components/documents/DocumentSheet";
import {
  CitationChip,
  type FindingsById,
} from "@/components/investigation/CitationChip";
import { QtypeBadge } from "@/components/investigation/OpenQuestionsPanel";
import { Badge } from "@/components/ui/badge";
import type { AlternativesSection, InvestigationQuestion } from "@/lib/api";

/**
 * "Why they did not do that way" — alternatives are mined, never invented:
 * every grounded item carries evidence-backed finding chips. Alternatives the
 * corpus couldn't answer render as explicit "unanswered" rows (that absence
 * IS the answer surface).
 */
export function AlternativesPanel({
  section,
  questions,
  findings,
  onOpenDocument,
}: {
  section: AlternativesSection;
  questions: InvestigationQuestion[];
  findings: FindingsById;
  onOpenDocument: (target: DocumentSheetTarget) => void;
}) {
  const items = section.items ?? [];
  const unansweredIds = section.unanswered_question_ids ?? [];
  const questionById = new Map(questions.map((q) => [q.id, q]));

  if (items.length === 0 && unansweredIds.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No documented alternatives were found in the sources.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {items.length > 0 ? (
        <ul className="divide-y rounded-xl border bg-card">
          {items.map((item, i) => (
            <li key={i} className="flex flex-col gap-1.5 px-3 py-3">
              <p className="text-sm font-medium leading-6">{item.title}</p>
              {item.description ? (
                <p className="max-w-prose text-sm leading-6 text-muted-foreground">
                  {item.description}
                </p>
              ) : null}
              <div className="flex flex-wrap items-center gap-1.5">
                {item.by_whom_entity_id ? (
                  <Link
                    href={`/entity/${item.by_whom_entity_id}`}
                    className="text-xs text-primary underline-offset-4 hover:underline"
                  >
                    proposed by
                  </Link>
                ) : null}
                {(item.finding_ids ?? []).map((findingId) => (
                  <CitationChip
                    key={findingId}
                    findingId={findingId}
                    findings={findings}
                    onOpenDocument={onOpenDocument}
                  />
                ))}
              </div>
            </li>
          ))}
        </ul>
      ) : null}

      {unansweredIds.length > 0 ? (
        <ul className="divide-y rounded-xl border border-dashed bg-muted/20">
          {unansweredIds.map((questionId) => {
            const question = questionById.get(questionId);
            return (
              <li
                key={questionId}
                className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5"
              >
                <p className="min-w-0 flex-1 text-sm text-muted-foreground">
                  {question?.text ?? `Question #${questionId}`}
                </p>
                <span className="flex shrink-0 items-center gap-1.5">
                  {question ? <QtypeBadge qtype={question.qtype} /> : null}
                  <Badge
                    variant="secondary"
                    className="bg-muted text-muted-foreground"
                  >
                    unanswered
                  </Badge>
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
