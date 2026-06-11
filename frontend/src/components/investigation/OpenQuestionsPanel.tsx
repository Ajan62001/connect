"use client";

import Link from "next/link";
import { ArrowRightIcon, Loader2Icon, TelescopeIcon } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type {
  InvestigationQuestion,
  QuestionStatus,
  QuestionType,
} from "@/lib/api";
import { useInvestigateQuestion } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** "why_not_alternative" -> "why not alternative". */
export function QtypeBadge({
  qtype,
  className,
}: {
  qtype: QuestionType;
  className?: string;
}) {
  return (
    <Badge variant="outline" className={className}>
      {qtype.replace(/_/g, " ")}
    </Badge>
  );
}

const STATUS_STYLES: Record<QuestionStatus, string> = {
  open: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  partial: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  answered:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  dropped:
    "border-border bg-transparent text-muted-foreground line-through decoration-muted-foreground/50",
};

export function QuestionStatusChip({
  status,
  className,
}: {
  status: QuestionStatus;
  className?: string;
}) {
  return (
    <Badge
      variant="secondary"
      className={cn(STATUS_STYLES[status] ?? "bg-muted text-muted-foreground", className)}
    >
      {status}
    </Badge>
  );
}

/**
 * The manual-recursion button (USER DECISION: click-only, never auto-spawn).
 * POST /api/questions/{id}/investigate, then a toast linking to the child.
 */
function InvestigateThisButton({
  question,
  investigationId,
}: {
  question: InvestigationQuestion;
  investigationId: number;
}) {
  const recurse = useInvestigateQuestion();
  return (
    <Button
      variant="outline"
      size="xs"
      disabled={recurse.isPending}
      onClick={() =>
        recurse.mutate(
          { questionId: question.id, parentInvestigationId: investigationId },
          {
            onSuccess: (accepted) =>
              toast.success("Follow-up investigation started", {
                description: question.text,
                action: (
                  <Button
                    size="xs"
                    render={
                      <Link href={`/investigation/${accepted.investigation_id}`} />
                    }
                  >
                    Open
                  </Button>
                ),
              }),
            onError: (error) =>
              toast.error("Could not start follow-up", {
                description: error.message,
              }),
          },
        )
      }
    >
      {recurse.isPending ? (
        <Loader2Icon className="animate-spin" data-icon="inline-start" />
      ) : (
        <TelescopeIcon data-icon="inline-start" />
      )}
      Investigate this
    </Button>
  );
}

function QuestionRow({
  question,
  investigationId,
}: {
  question: InvestigationQuestion;
  investigationId: number;
}) {
  const canRecurse =
    question.spawned_dossier_id === null &&
    (question.status === "open" || question.status === "partial");
  return (
    <li className="flex flex-col gap-1.5 px-3 py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="min-w-0 flex-1 text-sm font-medium leading-6">
          {question.text}
        </p>
        <span className="flex shrink-0 items-center gap-1.5">
          <QtypeBadge qtype={question.qtype} />
          <QuestionStatusChip status={question.status} />
        </span>
      </div>
      {question.answer_summary ? (
        <p className="max-w-prose text-sm leading-6 text-muted-foreground">
          {question.answer_summary}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {canRecurse ? (
          <InvestigateThisButton
            question={question}
            investigationId={investigationId}
          />
        ) : null}
        {question.spawned_dossier_id !== null ? (
          <Button
            variant="ghost"
            size="xs"
            render={<Link href={`/investigation/${question.spawned_dossier_id}`} />}
          >
            Follow-up investigation
            <ArrowRightIcon data-icon="inline-end" />
          </Button>
        ) : null}
      </div>
    </li>
  );
}

/**
 * The investigation's question ledger — the loop's work queue, rendered live.
 * Remaining open questions are a product feature, each with the manual
 * [Investigate this] recursion button.
 */
export function OpenQuestionsPanel({
  questions,
  investigationId,
}: {
  questions: InvestigationQuestion[];
  investigationId: number;
}) {
  if (questions.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No questions raised yet — they appear as the scope pass and the loop
        generate them.
      </p>
    );
  }
  // Open work first (priority-desc), then partial, answered, dropped.
  const order: Record<QuestionStatus, number> = {
    open: 0,
    partial: 1,
    answered: 2,
    dropped: 3,
  };
  const sorted = [...questions].sort(
    (a, b) =>
      (order[a.status] ?? 9) - (order[b.status] ?? 9) || b.priority - a.priority,
  );
  return (
    <ul className="divide-y rounded-xl border bg-card">
      {sorted.map((question) => (
        <QuestionRow
          key={question.id}
          question={question}
          investigationId={investigationId}
        />
      ))}
    </ul>
  );
}
