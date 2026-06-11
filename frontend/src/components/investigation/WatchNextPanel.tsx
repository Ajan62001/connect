"use client";

import { useState } from "react";
import { CalendarIcon, EyeIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { QtypeBadge } from "@/components/investigation/OpenQuestionsPanel";
import { Button } from "@/components/ui/button";
import type {
  InvestigationQuestion,
  WatchCreate,
  WatchKind,
  WatchNextItem,
  WatchNextSection,
} from "@/lib/api";
import { useCreateWatch } from "@/lib/queries";

const WATCH_KINDS: ReadonlySet<string> = new Set([
  "entity",
  "topic",
  "thread",
  "claim",
  "search",
] satisfies WatchKind[]);

/** Builds a valid WatchCreate from the (partial) suggestion + item text. */
function toWatchCreate(item: WatchNextItem): WatchCreate {
  const suggestion = item.watch_suggestion ?? {};
  const kind: WatchKind =
    suggestion.kind && WATCH_KINDS.has(suggestion.kind)
      ? suggestion.kind
      : "topic";
  return {
    kind,
    label: suggestion.label ?? item.text.slice(0, 120),
    query_fts: suggestion.query_fts ?? null,
    entity_id: suggestion.entity_id ?? null,
    promote: suggestion.promote ?? false,
  };
}

function WatchNextRow({
  item,
  question,
}: {
  item: WatchNextItem;
  question: InvestigationQuestion | undefined;
}) {
  const createWatch = useCreateWatch();
  const [created, setCreated] = useState(false);

  return (
    <li className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5">
      <div className="min-w-0 flex-1 space-y-0.5">
        <p className="text-sm leading-6">{item.text}</p>
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          {question ? (
            <>
              <QtypeBadge qtype={question.qtype} />
              <span className="min-w-0 truncate">{question.text}</span>
            </>
          ) : item.calendar_event_id ? (
            <span className="inline-flex items-center gap-1">
              <CalendarIcon className="size-3" aria-hidden />
              calendar event #{item.calendar_event_id}
            </span>
          ) : null}
        </p>
      </div>
      <Button
        variant="outline"
        size="xs"
        className="shrink-0"
        disabled={created || createWatch.isPending}
        onClick={() =>
          createWatch.mutate(toWatchCreate(item), {
            onSuccess: (watch) => {
              setCreated(true);
              toast.success("Watch created", { description: watch.label });
            },
            onError: (error) =>
              toast.error("Could not create watch", {
                description: error.message,
              }),
          })
        }
      >
        {createWatch.isPending ? (
          <Loader2Icon className="animate-spin" data-icon="inline-start" />
        ) : (
          <EyeIcon data-icon="inline-start" />
        )}
        {created ? "Watching" : "Watch"}
      </Button>
    </li>
  );
}

/**
 * "What to watch next" — each item anchors to an open question or a calendar
 * event (synthesis validator guarantee); +Watch posts the suggested payload
 * to the existing watch CRUD.
 */
export function WatchNextPanel({
  section,
  questions,
}: {
  section: WatchNextSection;
  questions: InvestigationQuestion[];
}) {
  const items = section.items ?? [];
  const questionById = new Map(questions.map((q) => [q.id, q]));
  if (items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        Nothing flagged to watch from this investigation.
      </p>
    );
  }
  return (
    <ul className="divide-y rounded-xl border bg-card">
      {items.map((item, i) => (
        <WatchNextRow
          key={i}
          item={item}
          question={
            item.question_id != null
              ? questionById.get(item.question_id)
              : undefined
          }
        />
      ))}
    </ul>
  );
}
