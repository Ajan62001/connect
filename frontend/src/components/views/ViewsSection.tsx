"use client";

import { useEffect, useRef, useState } from "react";
import { MessageSquareQuoteIcon, ZapIcon } from "lucide-react";

import { QueryError } from "@/components/shared/QueryError";
import { Skeleton } from "@/components/ui/skeleton";
import { EvolutionSummaryCard } from "@/components/views/EvolutionSummaryCard";
import { PositionCard, statementDomId } from "@/components/views/PositionCard";
import { ShiftBanner } from "@/components/views/ShiftBanner";
import { ApiError } from "@/lib/api";
import type { PositionShift, ViewsTopic } from "@/lib/api";
import { useEntityViews, useTopicViews } from "@/lib/queries";
import { cn } from "@/lib/utils";

function TopicChip({
  topic,
  selected,
  onSelect,
}: {
  topic: ViewsTopic;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      title={topic.latest_position ?? undefined}
      aria-pressed={selected}
      className={cn(
        "inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
        selected
          ? "border-primary/40 bg-primary/10 text-foreground"
          : "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
    >
      <span className="max-w-48 truncate">{topic.topic}</span>
      <span className="tabular-nums opacity-70">{topic.statement_count}</span>
      {topic.shift_count > 0 ? (
        <span
          className="inline-flex items-center gap-0.5 text-amber-600 dark:text-amber-400"
          title={`${topic.shift_count} position shift${
            topic.shift_count === 1 ? "" : "s"
          }`}
        >
          <ZapIcon className="size-3" aria-hidden />
          <span className="tabular-nums">{topic.shift_count}</span>
        </span>
      ) : null}
    </button>
  );
}

/**
 * One topic's view: evolution summary on top, then the newest-first statement
 * timeline with shift banners hung below their newer ("now") statement — i.e.
 * between the two cards they connect when those are adjacent.
 */
function TopicViewPanel({
  entityId,
  topic,
}: {
  entityId: number;
  topic: string;
}) {
  const views = useTopicViews(entityId, topic);

  // Transient highlight when an evolution-summary citation jumps to a card.
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  useEffect(
    () => () => {
      if (highlightTimer.current !== undefined)
        clearTimeout(highlightTimer.current);
    },
    [],
  );

  if (views.isPending) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-20 w-full rounded-lg" />
        <Skeleton className="h-24 w-full rounded-lg" />
        <Skeleton className="h-24 w-full rounded-lg" />
      </div>
    );
  }
  if (views.isError) {
    return (
      <QueryError error={views.error} onRetry={() => void views.refetch()} />
    );
  }

  const statements = views.data.statements ?? [];
  const shifts = views.data.shifts ?? [];
  const byId = new Map(statements.map((s) => [s.id, s]));
  const knownIds = new Set(statements.map((s) => s.id));

  // Banners keyed by their newer statement; shifts whose "now" side is not in
  // the list (defensive) render above the timeline instead of vanishing.
  const byTo = new Map<number, PositionShift[]>();
  const unanchored: PositionShift[] = [];
  for (const shift of shifts) {
    if (byId.has(shift.to_statement_id)) {
      const list = byTo.get(shift.to_statement_id) ?? [];
      list.push(shift);
      byTo.set(shift.to_statement_id, list);
    } else {
      unanchored.push(shift);
    }
  }

  const onCite = (statementId: number) => {
    globalThis.document
      .getElementById(statementDomId(statementId))
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
    setHighlightId(statementId);
    if (highlightTimer.current !== undefined)
      clearTimeout(highlightTimer.current);
    highlightTimer.current = setTimeout(() => setHighlightId(null), 2_000);
  };

  return (
    <div className="space-y-3">
      {views.data.evolution_summary ? (
        <EvolutionSummaryCard
          summary={views.data.evolution_summary}
          knownIds={knownIds}
          onCite={onCite}
        />
      ) : null}

      {unanchored.map((shift) => (
        <ShiftBanner
          key={shift.id}
          shift={shift}
          entityId={entityId}
          from={byId.get(shift.from_statement_id)}
          to={byId.get(shift.to_statement_id)}
        />
      ))}

      {statements.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No statements recorded on this topic yet.
        </p>
      ) : (
        <ol className="relative ml-1.5 space-y-3 border-l border-border pl-4">
          {statements.map((statement) => (
            <li key={statement.id} className="relative space-y-3">
              <span
                className="absolute top-4 -left-[21px] size-2 rounded-full border border-background bg-muted-foreground/50"
                aria-hidden
              />
              <PositionCard
                statement={statement}
                highlighted={highlightId === statement.id}
              />
              {(byTo.get(statement.id) ?? []).map((shift) => (
                <ShiftBanner
                  key={shift.id}
                  shift={shift}
                  entityId={entityId}
                  from={byId.get(shift.from_statement_id)}
                  to={byId.get(shift.to_statement_id)}
                />
              ))}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/**
 * "Views & statements" on the entity page (rendered when has_views): topic
 * chips with statement counts + shift badges; selecting a topic loads the
 * per-topic timeline inline. No topic is auto-selected — reading a topic can
 * trigger a governed summary regeneration, so it stays an explicit action.
 */
export function ViewsSection({ entityId }: { entityId: number }) {
  const views = useEntityViews(entityId);
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <section className="space-y-3">
      <h2 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
        <MessageSquareQuoteIcon
          className="size-4 text-muted-foreground"
          aria-hidden
        />
        Views &amp; statements
      </h2>

      {views.isPending ? (
        <div className="flex flex-wrap gap-1.5">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-7 w-28 rounded-full" />
          ))}
        </div>
      ) : views.isError ? (
        views.error instanceof ApiError && views.error.status === 404 ? (
          <p className="text-sm text-muted-foreground">
            The views API isn&apos;t available yet — attributed statements will
            appear here once the backend ships.
          </p>
        ) : (
          <QueryError
            error={views.error}
            onRetry={() => void views.refetch()}
          />
        )
      ) : (views.data.topics ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No attributed statements on record yet.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap gap-1.5">
            {views.data.topics.map((topic) => (
              <TopicChip
                key={topic.topic}
                topic={topic}
                selected={selected === topic.topic}
                onSelect={() =>
                  setSelected(selected === topic.topic ? null : topic.topic)
                }
              />
            ))}
          </div>
          {selected !== null ? (
            <TopicViewPanel entityId={entityId} topic={selected} />
          ) : (
            <p className="text-xs text-muted-foreground">
              Select a topic to see the statement timeline and how the position
              evolved.
            </p>
          )}
        </>
      )}
    </section>
  );
}
