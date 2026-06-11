"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";

import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDay, relativeTime } from "@/lib/format";
import { useEvent } from "@/lib/queries";

/** Minimum an event row needs; doc_count is absent on entity-page events. */
export interface TimelineEvent {
  id: number;
  title: string;
  event_type: string;
  occurred_on: string;
  doc_count?: number;
}

/** Small outline chip for the controlled event-type vocabulary. */
export function EventTypeChip({ type }: { type: string }) {
  return <Badge variant="outline">{type.replace(/_/g, " ")}</Badge>;
}

/** Lazy-loads an expanded event's documents via GET /api/events/{id}. */
function EventDocuments({ eventId }: { eventId: number }) {
  const event = useEvent(eventId);

  if (event.isPending) {
    return (
      <div className="mt-2 space-y-2 rounded-lg border bg-muted/30 p-3">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    );
  }
  if (event.isError) {
    return (
      <div className="mt-2">
        <QueryError error={event.error} onRetry={() => void event.refetch()} />
      </div>
    );
  }

  const documents = event.data.documents ?? [];
  if (documents.length === 0) {
    return (
      <p className="mt-2 text-xs text-muted-foreground">
        No documents attached to this event.
      </p>
    );
  }

  return (
    <ul className="mt-2 space-y-1.5 rounded-lg border bg-muted/30 p-3">
      {documents.map((doc) => (
        <li key={doc.id} className="flex items-baseline gap-3">
          <Link
            href={`/documents/${doc.id}`}
            className="min-w-0 flex-1 truncate text-sm text-primary underline-offset-4 hover:underline"
          >
            {doc.title ?? "(untitled document)"}
          </Link>
          <span className="shrink-0 text-xs text-muted-foreground">
            {[doc.source_name, relativeTime(doc.published_at ?? doc.fetched_at)]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </li>
      ))}
    </ul>
  );
}

function EventTimelineRow({
  event,
  expandable,
}: {
  event: TimelineEvent;
  expandable: boolean;
}) {
  const [open, setOpen] = useState(false);

  return (
    <li className="relative pb-6 pl-6 last:pb-0">
      <span
        className="absolute top-1 left-0 size-2.5 -translate-x-1/2 rounded-full border-2 border-background bg-primary/70"
        aria-hidden
      />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-xs text-muted-foreground tabular-nums">
          {formatDay(event.occurred_on)}
        </span>
        <EventTypeChip type={event.event_type} />
        {typeof event.doc_count === "number" ? (
          <span className="text-xs text-muted-foreground">
            {event.doc_count} doc{event.doc_count === 1 ? "" : "s"}
          </span>
        ) : null}
        <Link
          href={`/event/${event.id}`}
          className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          open
        </Link>
      </div>
      {expandable ? (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="mt-1 flex cursor-pointer items-start gap-1 text-left text-sm font-medium underline-offset-4 hover:underline"
        >
          {open ? (
            <ChevronDownIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          ) : (
            <ChevronRightIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          )}
          {event.title}
        </button>
      ) : (
        <Link
          href={`/event/${event.id}`}
          className="mt-1 block text-sm font-medium underline-offset-4 hover:underline"
        >
          {event.title}
        </Link>
      )}
      {expandable && open ? <EventDocuments eventId={event.id} /> : null}
    </li>
  );
}

/**
 * Vertical event timeline. `expandable` rows toggle an inline document list
 * (thread page); otherwise the title links straight to the event page
 * (entity page panel). Callers pass events already in display order.
 */
export function EventTimeline({
  events,
  expandable = false,
}: {
  events: TimelineEvent[];
  expandable?: boolean;
}) {
  return (
    <ol className="ml-1.5 border-l">
      {events.map((event) => (
        <EventTimelineRow key={event.id} event={event} expandable={expandable} />
      ))}
    </ol>
  );
}
