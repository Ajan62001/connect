"use client";

import { useState } from "react";
import {
  DatabaseIcon,
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { SourceForm } from "@/components/sources/SourceForm";
import { SourceHealthBadge } from "@/components/sources/SourceHealthBadge";
import { TierDots } from "@/components/sources/TierDots";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { Source, SourceCreate } from "@/lib/api";
import {
  useCreateSource,
  useDeleteSource,
  usePollSource,
  useSources,
  useUpdateSource,
} from "@/lib/queries";

function SourcesSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="flex items-center gap-4">
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-5 w-14" />
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-5 w-10" />
          <Skeleton className="h-4 w-24" />
        </div>
      ))}
    </div>
  );
}

/** Source types the backend can poll on demand (registry POLLABLE_TYPES). */
const POLLABLE_TYPES: Source["type"][] = ["rss", "twitter", "telegram"];

const TYPE_BADGE_LABELS: Partial<Record<Source["type"], string>> = {
  twitter: "X / Twitter",
  telegram: "Telegram",
};

function SourceRow({
  source,
  onEdit,
  onDelete,
}: {
  source: Source;
  onEdit: (source: Source) => void;
  onDelete: (source: Source) => void;
}) {
  const updateSource = useUpdateSource();
  const pollSource = usePollSource();

  return (
    <TableRow>
      <TableCell className="max-w-0">
        <p className="truncate font-medium">{source.name}</p>
        {source.notes ? (
          <p className="truncate text-xs text-muted-foreground">{source.notes}</p>
        ) : null}
      </TableCell>
      <TableCell>
        <span className="flex items-center gap-1.5">
          <Badge variant="outline">
            {TYPE_BADGE_LABELS[source.type] ?? source.type}
          </Badge>
          {source.t1_exempt ? (
            <Badge
              variant="secondary"
              title="High-volume source — skipped by T1 enrichment unless a watch hits"
            >
              T1-exempt
            </Badge>
          ) : null}
        </span>
      </TableCell>
      <TableCell>
        <TierDots tier={source.credibility_tier} />
      </TableCell>
      <TableCell>
        <Switch
          checked={source.enabled}
          aria-label={`${source.enabled ? "Disable" : "Enable"} ${source.name}`}
          disabled={updateSource.isPending}
          onCheckedChange={(checked) =>
            updateSource.mutate(
              { id: source.id, payload: { enabled: checked } },
              {
                onError: (error) =>
                  toast.error("Update failed", { description: error.message }),
              },
            )
          }
        />
      </TableCell>
      <TableCell>
        <SourceHealthBadge source={source} />
      </TableCell>
      <TableCell className="text-right tabular-nums">{source.doc_count}</TableCell>
      <TableCell>
        <span className="flex items-center justify-end gap-1">
          {POLLABLE_TYPES.includes(source.type) ? (
            <Button
              variant="ghost"
              size="icon-sm"
              title="Poll now"
              aria-label={`Poll ${source.name} now`}
              disabled={pollSource.isPending}
              onClick={() =>
                pollSource.mutate(source.id, {
                  onSuccess: ({ job_id }) =>
                    toast.success("Poll queued", {
                      description: `${source.name} · job #${job_id}`,
                    }),
                  onError: (error) =>
                    toast.error("Poll failed", { description: error.message }),
                })
              }
            >
              <RefreshCwIcon />
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon-sm"
            title="Edit"
            aria-label={`Edit ${source.name}`}
            onClick={() => onEdit(source)}
          >
            <PencilIcon />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            title="Delete"
            aria-label={`Delete ${source.name}`}
            onClick={() => onDelete(source)}
          >
            <Trash2Icon />
          </Button>
        </span>
      </TableCell>
    </TableRow>
  );
}

export default function SourcesPage() {
  const sources = useSources();
  const createSource = useCreateSource();
  const updateSource = useUpdateSource();
  const deleteSource = useDeleteSource();

  const [addOpen, setAddOpen] = useState(false);
  const [editing, setEditing] = useState<Source | null>(null);
  const [deleting, setDeleting] = useState<Source | null>(null);

  function handleCreate(payload: SourceCreate) {
    createSource.mutate(payload, {
      onSuccess: (source) => {
        toast.success("Source added", { description: source.name });
        setAddOpen(false);
      },
      onError: (error) =>
        toast.error("Could not add source", { description: error.message }),
    });
  }

  function handleEdit(payload: SourceCreate) {
    if (!editing) return;
    updateSource.mutate(
      {
        id: editing.id,
        payload: {
          name: payload.name,
          config: payload.config,
          credibility_tier: payload.credibility_tier,
          notes: payload.notes,
        },
      },
      {
        onSuccess: (source) => {
          toast.success("Source updated", { description: source.name });
          setEditing(null);
        },
        onError: (error) =>
          toast.error("Update failed", { description: error.message }),
      },
    );
  }

  function handleDelete() {
    if (!deleting) return;
    deleteSource.mutate(deleting.id, {
      onSuccess: () => {
        toast.success("Source deleted", { description: deleting.name });
        setDeleting(null);
      },
      onError: (error) =>
        toast.error("Delete failed", { description: error.message }),
    });
  }

  return (
    <>
      <PageHeader
        title="Sources"
        description="The registry the poller works through — every document traces back to one of these."
        actions={
          <Button onClick={() => setAddOpen(true)}>
            <PlusIcon data-icon="inline-start" />
            Add source
          </Button>
        }
      />

      {sources.isPending ? (
        <SourcesSkeleton />
      ) : sources.isError ? (
        <QueryError error={sources.error} onRetry={() => void sources.refetch()} />
      ) : sources.data.length === 0 ? (
        <EmptyState
          icon={DatabaseIcon}
          title="No sources registered"
          description="Add an RSS feed to start growing the corpus automatically. Seeded India sources appear here once the backend has run."
          action={
            <Button onClick={() => setAddOpen(true)}>
              <PlusIcon data-icon="inline-start" />
              Add source
            </Button>
          }
        />
      ) : (
        <div className="rounded-xl border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead className="w-44">Type</TableHead>
                <TableHead className="w-20">Tier</TableHead>
                <TableHead className="w-20">Enabled</TableHead>
                <TableHead className="w-48">Health</TableHead>
                <TableHead className="w-16 text-right">Docs</TableHead>
                <TableHead className="w-28" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {sources.data.map((source) => (
                <SourceRow
                  key={source.id}
                  source={source}
                  onEdit={setEditing}
                  onDelete={setDeleting}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Add dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Add source</DialogTitle>
            <DialogDescription>
              Register a source for the poller. Test the feed before saving.
            </DialogDescription>
          </DialogHeader>
          <SourceForm
            submitting={createSource.isPending}
            submitLabel="Add source"
            onSubmit={handleCreate}
          />
        </DialogContent>
      </Dialog>

      {/* Edit dialog — keyed so the form re-initializes per source */}
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit source</DialogTitle>
            <DialogDescription>{editing?.name}</DialogDescription>
          </DialogHeader>
          {editing ? (
            <SourceForm
              key={editing.id}
              source={editing}
              submitting={updateSource.isPending}
              submitLabel="Save changes"
              onSubmit={handleEdit}
            />
          ) : null}
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete source?</DialogTitle>
            <DialogDescription>
              {deleting
                ? `“${deleting.name}” will be removed from the registry. Its ${deleting.doc_count} stored document${deleting.doc_count === 1 ? "" : "s"} stay in the corpus.`
                : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteSource.isPending}
              onClick={handleDelete}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
