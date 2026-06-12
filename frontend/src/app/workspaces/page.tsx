"use client";

import { useState } from "react";
import Link from "next/link";
import { LayoutGridIcon, Loader2Icon, SearchIcon, TagIcon } from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import {
  OwnerByline,
  VisibilityBadge,
  VisibilityToggle,
} from "@/components/shared/Visibility";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type { Visibility } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useCreateWorkspace, useWorkspaces } from "@/lib/queries";

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function Composer() {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [topics, setTopics] = useState("");
  const [query, setQuery] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("shared");
  const create = useCreateWorkspace();

  const submit = () => {
    if (!name.trim()) return;
    create.mutate(
      {
        name: name.trim(),
        description: description.trim(),
        topics: splitCsv(topics),
        query_fts: query.trim() || null,
        visibility,
      },
      {
        onSuccess: () => {
          toast.success("Workspace created");
          setName("");
          setDescription("");
          setTopics("");
          setQuery("");
          setVisibility("shared");
        },
        onError: (e) =>
          toast.error("Could not create workspace", { description: e.message }),
      },
    );
  };

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <LayoutGridIcon className="size-4" />
          New workspace
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Workspace name (e.g. RBI & Monetary Policy)"
          maxLength={120}
        />
        <Textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What is this workspace about? (optional)"
          className="min-h-16"
        />
        <Input
          value={topics}
          onChange={(e) => setTopics(e.target.value)}
          placeholder="Focus topics, comma-separated (e.g. monetary-policy, banking)"
        />
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
        <Button onClick={submit} disabled={!name.trim() || create.isPending}>
          {create.isPending ? (
            <Loader2Icon className="animate-spin" data-icon="inline-start" />
          ) : null}
          Create workspace
        </Button>
      </CardContent>
    </Card>
  );
}

export default function WorkspacesPage() {
  const workspaces = useWorkspaces();

  return (
    <>
      <PageHeader
        title="Workspaces"
        description="Focused lenses over the news — each with its own feed, findings and posts."
      />
      <Composer />

      {workspaces.isPending ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
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
          description="Create a workspace above to focus the news around a topic."
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
