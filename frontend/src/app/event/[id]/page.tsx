"use client";

import { use } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeftIcon,
  CalendarOffIcon,
  FileTextIcon,
  GitBranchIcon,
} from "lucide-react";

import { DocumentTable } from "@/components/documents/DocumentTable";
import { EntityPill } from "@/components/entities/EntityPill";
import { EventTypeChip } from "@/components/events/EventTimeline";
import { InvestigateButton } from "@/components/investigation/InvestigateButton";
import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDay } from "@/lib/format";
import { useEvent } from "@/lib/queries";

function EventSkeleton() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-7 w-80" />
        <Skeleton className="h-4 w-56" />
      </div>
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/** Lightweight event surface: header + documents + entities. */
export default function EventPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const eventId = Number(id);
  const event = useEvent(eventId);

  if (!Number.isFinite(eventId)) {
    return (
      <EmptyState
        icon={CalendarOffIcon}
        title="Invalid event id"
        description={`“${id}” is not an event id.`}
      />
    );
  }

  return (
    <>
      <Button variant="ghost" size="sm" onClick={() => router.back()}>
        <ArrowLeftIcon data-icon="inline-start" />
        Back
      </Button>

      {event.isPending ? (
        <EventSkeleton />
      ) : event.isError ? (
        <QueryError error={event.error} onRetry={() => void event.refetch()} />
      ) : (
        <>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1.5">
              <h1 className="text-xl font-semibold tracking-tight">
                {event.data.event.title}
              </h1>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
                <EventTypeChip type={event.data.event.event_type} />
                <span>{formatDay(event.data.event.occurred_on)}</span>
                {event.data.event.geo_scope ? (
                  <Badge variant="secondary">{event.data.event.geo_scope}</Badge>
                ) : null}
                <span>
                  {event.data.event.doc_count} document
                  {event.data.event.doc_count === 1 ? "" : "s"}
                </span>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <InvestigateButton seed={{ event_id: eventId }} />
              {event.data.event.story_id !== null ? (
                <Button
                  variant="outline"
                  size="sm"
                  render={<Link href={`/thread/${event.data.event.story_id}`} />}
                >
                  <GitBranchIcon data-icon="inline-start" />
                  View thread
                </Button>
              ) : null}
            </div>
          </div>

          {event.data.event.summary ? (
            <p className="max-w-prose text-sm leading-6">
              {event.data.event.summary}
            </p>
          ) : null}

          {(event.data.entities ?? []).length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {event.data.entities.map((entity) => (
                <EntityPill key={entity.id} entity={entity} />
              ))}
            </div>
          ) : null}

          <section className="space-y-3">
            <h2 className="text-sm font-semibold tracking-tight">Documents</h2>
            {(event.data.documents ?? []).length === 0 ? (
              <EmptyState
                icon={FileTextIcon}
                title="No documents attached"
                description="Documents land here as T2 promotion assigns them to this event."
              />
            ) : (
              <DocumentTable items={event.data.documents} />
            )}
          </section>
        </>
      )}
    </>
  );
}
