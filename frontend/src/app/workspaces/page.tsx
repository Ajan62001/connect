"use client";

import { useState } from "react";
import Link from "next/link";
import {
  LayoutGridIcon,
  Loader2Icon,
  PlusIcon,
  SearchIcon,
  TagIcon,
} from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { TopicChooser } from "@/components/workspace/TopicChooser";
import { QueryError } from "@/components/shared/QueryError";
import {
  OwnerByline,
  VisibilityBadge,
  VisibilityToggle,
} from "@/components/shared/Visibility";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type { Visibility } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useCreateWorkspace, useWorkspaces } from "@/lib/queries";

function NewWorkspaceDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [topics, setTopics] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("shared");
  const create = useCreateWorkspace();

  const submit = () => {
    if (!name.trim()) return;
    create.mutate(
      {
        name: name.trim(),
        description: description.trim(),
        topics,
        query_fts: query.trim() || null,
        visibility,
      },
      {
        onSuccess: () => {
          toast.success("Workspace created");
          setName("");
          setDescription("");
          setTopics([]);
          setQuery("");
          setVisibility("shared");
          onOpenChange(false);
        },
        onError: (e) =>
          toast.error("Could not create workspace", { description: e.message }),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>New workspace</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Workspace name (e.g. RBI & Monetary Policy)"
            maxLength={120}
            autoFocus
          />
          <Textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What is this workspace about? (optional)"
            className="min-h-16"
          />
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-muted-foreground">
              Focus topics — pick any that this workspace should track
            </p>
            <TopicChooser value={topics} onChange={setTopics} />
          </div>
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Focus search query (optional, e.g. repo rate)"
          />
          <VisibilityToggle
            value={visibility}
            onChange={setVisibility}
            kind="workspace"
          />
        </div>
        <DialogFooter>
          <Button onClick={submit} disabled={!name.trim() || create.isPending}>
            {create.isPending ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : null}
            Create workspace
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function WorkspacesPage() {
  const workspaces = useWorkspaces();
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <>
      <PageHeader
        title="Workspaces"
        description="Focused lenses over the news — each its own assistant, feed, findings and posts."
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <PlusIcon data-icon="inline-start" />
            New workspace
          </Button>
        }
      />
      <NewWorkspaceDialog open={createOpen} onOpenChange={setCreateOpen} />

      {workspaces.isPending ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      ) : workspaces.isError ? (
        <QueryError
          error={workspaces.error}
          onRetry={() => void workspaces.refetch()}
        />
      ) : workspaces.data.length === 0 ? (
        <EmptyState
          icon={LayoutGridIcon}
          title="No workspaces yet"
          description="Create a workspace to focus the news around a topic — it gets its own assistant, feed and findings."
          action={
            <Button onClick={() => setCreateOpen(true)}>
              <PlusIcon data-icon="inline-start" />
              New workspace
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {workspaces.data.map((ws) => (
            <Card key={ws.id} className="transition-colors hover:bg-accent/40">
              <CardContent className="space-y-2 py-4">
                <Link href={`/workspaces/${ws.id}`} className="block">
                  <h3 className="font-medium hover:underline">{ws.name}</h3>
                </Link>
                {ws.description ? (
                  <p className="line-clamp-2 text-sm text-muted-foreground">
                    {ws.description}
                  </p>
                ) : null}
                <div className="flex flex-wrap items-center gap-1.5">
                  {ws.topics.map((t) => (
                    <Badge key={t} variant="secondary">
                      <TagIcon className="size-3" />
                      {t}
                    </Badge>
                  ))}
                  {ws.query_fts ? (
                    <Badge variant="outline">
                      <SearchIcon className="size-3" />
                      {ws.query_fts}
                    </Badge>
                  ) : null}
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span title={ws.created_at}>{relativeTime(ws.created_at)}</span>
                  <OwnerByline ownerId={ws.owner_id} ownerName={ws.owner_name} />
                  <VisibilityBadge visibility={ws.visibility} />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
