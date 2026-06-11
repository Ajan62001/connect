"use client";

import { use, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowLeftIcon,
  EyeIcon,
  FileTextIcon,
  UserRoundXIcon,
} from "lucide-react";
import { toast } from "sonner";

import { DocumentTable } from "@/components/documents/DocumentTable";
import { EntityPill, EntityTypeChip } from "@/components/entities/EntityPill";
import { EmptyState } from "@/components/shared/EmptyState";
import { Paginator } from "@/components/shared/Paginator";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { CoOccurringEntity, EntityDetail } from "@/lib/api";
import { absoluteTime, relativeTime } from "@/lib/format";
import {
  useCreateWatch,
  useEntity,
  useEntityDocuments,
  useWatches,
} from "@/lib/queries";

const DOCS_PAGE_SIZE = 10;

/** Creates an entity watch; flips to a disabled "Watching" once one exists. */
function WatchButton({ entityId, name }: { entityId: number; name: string }) {
  const watches = useWatches();
  const createWatch = useCreateWatch();

  const existing = watches.data?.find(
    (watch) => watch.kind === "entity" && watch.entity_id === entityId,
  );
  if (existing) {
    return (
      <Button variant="outline" disabled title={`Watched as “${existing.label}”`}>
        <EyeIcon data-icon="inline-start" />
        Watching
      </Button>
    );
  }

  return (
    <Button
      disabled={createWatch.isPending}
      onClick={() =>
        createWatch.mutate(
          { kind: "entity", entity_id: entityId, label: name, promote: true },
          {
            onSuccess: (watch) =>
              toast.success("Watch created", { description: watch.label }),
            onError: (error) =>
              toast.error("Could not create watch", {
                description: error.message,
              }),
          },
        )
      }
    >
      <EyeIcon data-icon="inline-start" />
      Watch
    </Button>
  );
}

/**
 * Co-occurrence cloud: order (and therefore reading priority) is lift,
 * size/intensity is the raw together-count.
 */
function CoOccurrenceCloud({ items }: { items: CoOccurringEntity[] }) {
  const maxTogether = Math.max(...items.map((item) => item.together), 1);

  const sizeFor = (together: number): string => {
    const ratio = together / maxTogether;
    if (ratio > 0.66) return "px-3 py-1 text-sm";
    if (ratio > 0.33) return "px-2.5 py-0.5 text-xs";
    return "px-2 py-0.5 text-xs opacity-75";
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {items.map((item) => (
        <EntityPill
          key={item.entity.id}
          entity={item.entity}
          className={sizeFor(item.together)}
          title={`${item.entity.name} — ${item.together} shared document${
            item.together === 1 ? "" : "s"
          } · lift ${item.lift.toFixed(1)}`}
        />
      ))}
    </div>
  );
}

function StatLine({ detail }: { detail: EntityDetail }) {
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm text-muted-foreground">
      <span>
        <span className="font-semibold text-foreground">
          {detail.mention_count}
        </span>{" "}
        mention{detail.mention_count === 1 ? "" : "s"}
      </span>
      <span>
        <span className="font-semibold text-foreground">
          {detail.document_count}
        </span>{" "}
        document{detail.document_count === 1 ? "" : "s"}
      </span>
      {detail.first_seen_at ? (
        <span title={absoluteTime(detail.first_seen_at)}>
          first seen {relativeTime(detail.first_seen_at)}
        </span>
      ) : null}
      {detail.last_seen_at ? (
        <span title={absoluteTime(detail.last_seen_at)}>
          last seen {relativeTime(detail.last_seen_at)}
        </span>
      ) : null}
    </div>
  );
}

function DocumentsSection({
  entityId,
  detail,
}: {
  entityId: number;
  detail: EntityDetail;
}) {
  const [showAll, setShowAll] = useState(false);
  const [page, setPage] = useState(1);
  const allDocs = useEntityDocuments(
    entityId,
    { page, page_size: DOCS_PAGE_SIZE },
    showAll,
  );

  const recent = detail.documents ?? [];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold tracking-tight">
          {showAll ? "All documents" : "Recent documents"}
        </h2>
        {!showAll && detail.document_count > recent.length ? (
          <Button variant="ghost" size="sm" onClick={() => setShowAll(true)}>
            View all {detail.document_count}
          </Button>
        ) : null}
      </div>

      {!showAll ? (
        recent.length === 0 ? (
          <EmptyState
            icon={FileTextIcon}
            title="No documents yet"
            description="Documents mentioning this entity will appear here as enrichment runs."
          />
        ) : (
          <DocumentTable items={recent} />
        )
      ) : allDocs.isPending ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-5 w-full" />
          ))}
        </div>
      ) : allDocs.isError ? (
        <QueryError error={allDocs.error} onRetry={() => void allDocs.refetch()} />
      ) : (
        <>
          <DocumentTable items={allDocs.data.items} />
          <Paginator
            page={allDocs.data.page}
            pageSize={allDocs.data.page_size}
            total={allDocs.data.total}
            onPageChange={setPage}
          />
        </>
      )}
    </section>
  );
}

function EntitySkeleton() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-96" />
      </div>
      <Skeleton className="h-4 w-80" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

export default function EntityPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const entityId = Number(id);
  const entity = useEntity(entityId);

  if (!Number.isFinite(entityId)) {
    return (
      <EmptyState
        icon={UserRoundXIcon}
        title="Invalid entity id"
        description={`“${id}” is not an entity id.`}
      />
    );
  }

  return (
    <>
      <Button variant="ghost" size="sm" onClick={() => router.back()}>
        <ArrowLeftIcon data-icon="inline-start" />
        Back
      </Button>

      {entity.isPending ? (
        <EntitySkeleton />
      ) : entity.isError ? (
        <QueryError error={entity.error} onRetry={() => void entity.refetch()} />
      ) : (
        <>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-2.5">
                <h1 className="text-xl font-semibold tracking-tight">
                  {entity.data.entity.name}
                </h1>
                <EntityTypeChip type={entity.data.entity.entity_type} />
              </div>
              {entity.data.entity.aliases.length > 0 ? (
                <p className="text-sm text-muted-foreground">
                  Also known as: {entity.data.entity.aliases.join(" · ")}
                </p>
              ) : null}
              {entity.data.entity.description ? (
                <p className="max-w-prose text-sm text-muted-foreground">
                  {entity.data.entity.description}
                </p>
              ) : null}
            </div>
            <WatchButton entityId={entityId} name={entity.data.entity.name} />
          </div>

          <StatLine detail={entity.data} />

          {entity.data.topics.length > 0 ? (
            <section className="space-y-2">
              <h2 className="text-sm font-semibold tracking-tight">Topics</h2>
              <div className="flex flex-wrap gap-1.5">
                {entity.data.topics.map((topic) => (
                  <Badge key={topic.topic} variant="secondary">
                    {topic.topic}
                    <span className="text-muted-foreground">{topic.count}</span>
                  </Badge>
                ))}
              </div>
            </section>
          ) : null}

          {entity.data.co_occurring.length > 0 ? (
            <section className="space-y-2">
              <h2 className="text-sm font-semibold tracking-tight">
                Appears alongside
              </h2>
              <CoOccurrenceCloud items={entity.data.co_occurring} />
            </section>
          ) : null}

          <DocumentsSection entityId={entityId} detail={entity.data} />
        </>
      )}
    </>
  );
}
