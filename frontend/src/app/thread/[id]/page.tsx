"use client";

import { use } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeftIcon, GitBranchIcon, MilestoneIcon } from "lucide-react";

import { EntityPill } from "@/components/entities/EntityPill";
import { EventTimeline } from "@/components/events/EventTimeline";
import { InvestigateButton } from "@/components/investigation/InvestigateButton";
import { TellStoryButton } from "@/components/story/TellStoryButton";
import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { absoluteTime, relativeTime } from "@/lib/format";
import { useThread } from "@/lib/queries";

function ThreadSkeleton() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-7 w-80" />
        <Skeleton className="h-4 w-64" />
      </div>
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/** A story thread: header, summary, entities, chronological event timeline. */
export default function ThreadPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const threadId = Number(id);
  const thread = useThread(threadId);

  if (!Number.isFinite(threadId)) {
    return (
      <EmptyState
        icon={GitBranchIcon}
        title="Invalid thread id"
        description={`“${id}” is not a thread id.`}
      />
    );
  }

  return (
    <>
      <Button variant="ghost" size="sm" onClick={() => router.back()}>
        <ArrowLeftIcon data-icon="inline-start" />
        Back
      </Button>

      {thread.isPending ? (
        <ThreadSkeleton />
      ) : thread.isError ? (
        <QueryError error={thread.error} onRetry={() => void thread.refetch()} />
      ) : (
        <>
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2.5">
              <h1 className="text-xl font-semibold tracking-tight">
                {thread.data.story.title}
              </h1>
              <Badge
                variant={
                  thread.data.story.status === "active" ? "secondary" : "outline"
                }
              >
                {thread.data.story.status}
              </Badge>
              <span className="ml-auto flex items-center gap-2">
                <TellStoryButton source={{ story_id: threadId }} />
                <InvestigateButton seed={{ story_id: threadId }} />
              </span>
            </div>
            <p
              className="text-sm text-muted-foreground"
              title={absoluteTime(thread.data.story.updated_at)}
            >
              {(thread.data.events ?? []).length} event
              {(thread.data.events ?? []).length === 1 ? "" : "s"} ·{" "}
              {thread.data.story.doc_count} document
              {thread.data.story.doc_count === 1 ? "" : "s"} · updated{" "}
              {relativeTime(thread.data.story.updated_at)}
            </p>
          </div>

          {thread.data.summary ? (
            <p className="max-w-prose text-sm leading-6">{thread.data.summary}</p>
          ) : null}

          {(thread.data.entities ?? []).length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {thread.data.entities.map((entity) => (
                <EntityPill key={entity.id} entity={entity} />
              ))}
            </div>
          ) : null}

          <section className="space-y-3">
            <h2 className="text-sm font-semibold tracking-tight">Timeline</h2>
            {(thread.data.events ?? []).length === 0 ? (
              <EmptyState
                icon={MilestoneIcon}
                title="No events in this thread yet"
                description="Events join the thread as T2 promotion clusters new documents."
              />
            ) : (
              <EventTimeline events={thread.data.events} expandable />
            )}
          </section>
        </>
      )}
    </>
  );
}
