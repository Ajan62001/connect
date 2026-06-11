"use client";

import { useState } from "react";
import Link from "next/link";
import {
  CheckIcon,
  EyeIcon,
  GitBranchIcon,
  HashIcon,
  PlusIcon,
  QuoteIcon,
  SearchIcon,
  Trash2Icon,
  UserRoundIcon,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { toast } from "sonner";

import { EntityAutocomplete } from "@/components/entities/EntityAutocomplete";
import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import type { EntityListItem, Watch, WatchKind } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  useCreateWatch,
  useDeleteWatch,
  useMarkWatchSeen,
  useUpdateWatch,
  useWatchBadges,
  useWatches,
} from "@/lib/queries";

const KIND_ICONS: Record<WatchKind, LucideIcon> = {
  entity: UserRoundIcon,
  topic: HashIcon,
  thread: GitBranchIcon,
  claim: QuoteIcon,
  search: SearchIcon,
};

const KIND_ITEMS: { value: WatchKind; label: string; disabled: boolean }[] = [
  { value: "topic", label: "Topic (FTS query, promotes matches)", disabled: false },
  { value: "search", label: "Saved search (FTS query)", disabled: false },
  { value: "entity", label: "Entity (alias match, promotes matches)", disabled: false },
  { value: "thread", label: "Thread — arrives in Phase 2", disabled: true },
  { value: "claim", label: "Claim — arrives in Phase 3", disabled: true },
];

const KIND_LABELS: Record<string, React.ReactNode> = Object.fromEntries(
  KIND_ITEMS.map((item) => [item.value, item.label]),
);

function CreateWatchDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const createWatch = useCreateWatch();
  const [kind, setKind] = useState<WatchKind>("topic");
  const [label, setLabel] = useState("");
  const [queryFts, setQueryFts] = useState("");
  const [entity, setEntity] = useState<EntityListItem | null>(null);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setKind("topic");
    setLabel("");
    setQueryFts("");
    setEntity(null);
    setError(null);
  }

  function submit() {
    if (kind === "entity" && !entity) {
      setError("Pick an entity to watch.");
      return;
    }
    // Entity watches inherit the entity's name as a label by default.
    const effectiveLabel =
      label.trim() || (kind === "entity" && entity ? entity.name : "");
    if (!effectiveLabel) {
      setError("Give the watch a label.");
      return;
    }
    if (kind !== "entity" && !queryFts.trim()) {
      setError("An FTS query is required for topic and search watches.");
      return;
    }
    setError(null);
    createWatch.mutate(
      kind === "entity"
        ? {
            kind,
            label: effectiveLabel,
            entity_id: entity!.id,
            promote: true,
          }
        : {
            kind,
            label: effectiveLabel,
            query_fts: queryFts.trim(),
            // Topic watches drive enrichment promotion; a saved search is just promote=false.
            promote: kind === "topic",
          },
      {
        onSuccess: (watch) => {
          toast.success("Watch created", { description: watch.label });
          onOpenChange(false);
          reset();
        },
        onError: (err) =>
          toast.error("Could not create watch", { description: err.message }),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New watch</DialogTitle>
          <DialogDescription>
            Topic and search watches FTS-match every new document at ingest;
            entity watches alias-match against the entity graph.
          </DialogDescription>
        </DialogHeader>
        <FieldGroup className="gap-4">
          <Field>
            <FieldLabel>Kind</FieldLabel>
            <Select
              items={KIND_LABELS}
              value={kind}
              onValueChange={(value) => {
                if (value === "topic" || value === "search" || value === "entity") {
                  setKind(value);
                  setError(null);
                }
              }}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Select a kind" />
              </SelectTrigger>
              <SelectContent>
                {KIND_ITEMS.map((item) => (
                  <SelectItem
                    key={item.value}
                    value={item.value}
                    disabled={item.disabled}
                  >
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          {kind === "entity" ? (
            <Field>
              <FieldLabel>Entity</FieldLabel>
              <EntityAutocomplete
                value={entity}
                onSelect={(picked) => {
                  setEntity(picked);
                  if (picked && !label.trim()) setLabel(picked.name);
                }}
              />
              <FieldDescription>
                New documents mentioning this entity (by name or alias) get
                flagged and promoted.
              </FieldDescription>
            </Field>
          ) : null}
          <Field>
            <FieldLabel htmlFor="watch-label">Label</FieldLabel>
            <Input
              id="watch-label"
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="e.g. SEBI enforcement"
            />
          </Field>
          {kind !== "entity" ? (
            <Field>
              <FieldLabel htmlFor="watch-query">FTS query</FieldLabel>
              <Input
                id="watch-query"
                value={queryFts}
                onChange={(event) => setQueryFts(event.target.value)}
                placeholder={'sebi AND ("show cause" OR penalty)'}
                className="font-mono text-sm"
              />
              <FieldDescription>
                SQLite FTS5 MATCH syntax: AND, OR, NOT, &quot;phrases&quot;,
                prefix*.
              </FieldDescription>
            </Field>
          ) : null}
          <FieldError errors={error ? [{ message: error }] : undefined} />
          <Button onClick={submit} disabled={createWatch.isPending}>
            Create watch
          </Button>
        </FieldGroup>
      </DialogContent>
    </Dialog>
  );
}

function WatchRow({ watch, unread }: { watch: Watch; unread: number }) {
  const updateWatch = useUpdateWatch();
  const deleteWatch = useDeleteWatch();
  const markSeen = useMarkWatchSeen();
  const Icon = KIND_ICONS[watch.kind] ?? EyeIcon;

  function onRowOpen() {
    if (unread > 0 && !markSeen.isPending) {
      markSeen.mutate(watch.id);
    }
  }

  return (
    <TableRow className="cursor-pointer" onClick={onRowOpen}>
      <TableCell>
        <span
          className="flex items-center gap-2 text-muted-foreground"
          title={watch.kind}
        >
          <Icon className="size-4" aria-hidden />
          <span className="text-xs">{watch.kind}</span>
        </span>
      </TableCell>
      <TableCell className="max-w-0">
        <p className="truncate font-medium">{watch.label}</p>
        {watch.query_fts ? (
          <code className="block truncate font-mono text-xs text-muted-foreground">
            {watch.query_fts}
          </code>
        ) : null}
        {watch.kind === "entity" && watch.entity_id !== null ? (
          <Link
            href={`/entity/${watch.entity_id}`}
            onClick={(event) => event.stopPropagation()}
            className="block truncate text-xs text-primary underline-offset-4 hover:underline"
          >
            entity #{watch.entity_id}
          </Link>
        ) : null}
      </TableCell>
      <TableCell>
        {unread > 0 ? (
          <Badge>{unread} new</Badge>
        ) : (
          <span
            className="text-xs text-muted-foreground"
            title={watch.last_seen_at ?? undefined}
          >
            {watch.last_seen_at
              ? `seen ${relativeTime(watch.last_seen_at)}`
              : "—"}
          </span>
        )}
      </TableCell>
      <TableCell onClick={(event) => event.stopPropagation()}>
        <Switch
          checked={watch.promote}
          aria-label="Promote matches to deeper enrichment"
          disabled={updateWatch.isPending}
          onCheckedChange={(checked) =>
            updateWatch.mutate(
              { id: watch.id, payload: { promote: checked } },
              {
                onError: (error) =>
                  toast.error("Update failed", { description: error.message }),
              },
            )
          }
        />
      </TableCell>
      <TableCell onClick={(event) => event.stopPropagation()}>
        <Switch
          checked={watch.muted}
          aria-label="Mute notifications for this watch"
          disabled={updateWatch.isPending}
          onCheckedChange={(checked) =>
            updateWatch.mutate(
              { id: watch.id, payload: { muted: checked } },
              {
                onError: (error) =>
                  toast.error("Update failed", { description: error.message }),
              },
            )
          }
        />
      </TableCell>
      <TableCell onClick={(event) => event.stopPropagation()}>
        <span className="flex items-center justify-end gap-1">
          {unread > 0 ? (
            <Button
              variant="ghost"
              size="icon-sm"
              title="Mark seen"
              aria-label={`Mark ${watch.label} seen`}
              disabled={markSeen.isPending}
              onClick={() => markSeen.mutate(watch.id)}
            >
              <CheckIcon />
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon-sm"
            title="Delete watch"
            aria-label={`Delete ${watch.label}`}
            disabled={deleteWatch.isPending}
            onClick={() =>
              deleteWatch.mutate(watch.id, {
                onSuccess: () =>
                  toast.success("Watch deleted", { description: watch.label }),
                onError: (error) =>
                  toast.error("Delete failed", { description: error.message }),
              })
            }
          >
            <Trash2Icon />
          </Button>
        </span>
      </TableCell>
    </TableRow>
  );
}

function WatchlistSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="flex items-center gap-4">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-5 w-12" />
          <Skeleton className="h-5 w-10" />
          <Skeleton className="h-5 w-10" />
        </div>
      ))}
    </div>
  );
}

export default function WatchlistPage() {
  const watches = useWatches();
  const badges = useWatchBadges();
  const [createOpen, setCreateOpen] = useState(false);

  const badgeFor = (id: number): number => badges.data?.[String(id)] ?? 0;

  return (
    <>
      <PageHeader
        title="Watchlist"
        description="Watches run against every document at ingest — FTS queries or entity aliases; matches are flagged in the feed and promoted."
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <PlusIcon data-icon="inline-start" />
            New watch
          </Button>
        }
      />

      {watches.isPending ? (
        <WatchlistSkeleton />
      ) : watches.isError ? (
        <QueryError error={watches.error} onRetry={() => void watches.refetch()} />
      ) : watches.data.length === 0 ? (
        <EmptyState
          icon={EyeIcon}
          title="Nothing being watched"
          description="Create a topic watch (an FTS5 query) and matching documents get flagged — and, from Phase 1, promoted to deeper enrichment."
          action={
            <Button onClick={() => setCreateOpen(true)}>
              <PlusIcon data-icon="inline-start" />
              New watch
            </Button>
          }
        />
      ) : (
        <div className="rounded-xl border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-24">Kind</TableHead>
                <TableHead>Label</TableHead>
                <TableHead className="w-28">Unread</TableHead>
                <TableHead className="w-20">Promote</TableHead>
                <TableHead className="w-20">Muted</TableHead>
                <TableHead className="w-20" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {watches.data.map((watch) => (
                <WatchRow
                  key={watch.id}
                  watch={watch}
                  unread={badgeFor(watch.id)}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <CreateWatchDialog open={createOpen} onOpenChange={setCreateOpen} />
    </>
  );
}
