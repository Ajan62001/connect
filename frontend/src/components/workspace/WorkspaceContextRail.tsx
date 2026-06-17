"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import {
  DatabaseIcon,
  ImageIcon,
  Loader2Icon,
  NotebookPenIcon,
  PlayIcon,
  RssIcon,
} from "lucide-react";
import { toast } from "sonner";

import { CollapsibleSection } from "@/components/workspace/CollapsibleSection";
import { WorkspaceDrafts } from "@/components/workspace/WorkspaceDrafts";
import { WorkspaceKnowledgeBase } from "@/components/workspace/WorkspaceKnowledgeBase";
import { WorkspaceTasks } from "@/components/workspace/WorkspaceTasks";
import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type { Post } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  useCreatePost,
  usePosts,
  useWorkspaceDocuments,
  useWorkspaceFeed,
  useWorkspaceSocialDrafts,
} from "@/lib/queries";

// --- Sources: the documents in focus --------------------------------------

function FocusedFeed({ workspaceId }: { workspaceId: number }) {
  const [page, setPage] = useState(1);
  const feed = useWorkspaceFeed(workspaceId, page);

  if (feed.isPending) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-14 w-full" />
        <Skeleton className="h-14 w-full" />
      </div>
    );
  }
  if (feed.isError) {
    return <QueryError error={feed.error} onRetry={() => void feed.refetch()} />;
  }
  if (feed.data.total === 0) {
    return (
      <EmptyState
        icon={RssIcon}
        title="Nothing in focus yet"
        description="No documents match this workspace's focus. Widen the topics or query, or wait for the feed to fill."
      />
    );
  }

  const { items, total, page: cur, page_size } = feed.data;
  const hasNext = cur * page_size < total;

  return (
    <div className="space-y-2">
      {items.map((doc) => (
        <div key={doc.id} className="rounded-md border bg-background px-3 py-2">
          <Link
            href={`/documents/${doc.id}`}
            className="text-sm font-medium hover:underline"
          >
            {doc.title ?? "(untitled document)"}
          </Link>
          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>{doc.source_name ?? "Manual ingest"}</span>
            <span title={doc.fetched_at}>{relativeTime(doc.fetched_at)}</span>
          </div>
        </div>
      ))}
      <div className="flex items-center justify-between pt-1 text-xs text-muted-foreground">
        <span>{total} in focus</span>
        <span className="flex gap-2">
          <Button
            variant="outline"
            size="xs"
            disabled={cur <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            Previous
          </Button>
          <Button
            variant="outline"
            size="xs"
            disabled={!hasNext}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </Button>
        </span>
      </div>
    </div>
  );
}

// --- Your work: findings ---------------------------------------------------

function FindingComposer({ workspaceId }: { workspaceId: number }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const create = useCreatePost();

  const submit = () => {
    if (!title.trim() || !body.trim()) return;
    create.mutate(
      { title: title.trim(), body: body.trim(), workspace_id: workspaceId },
      {
        onSuccess: () => {
          toast.success("Finding posted to workspace");
          setTitle("");
          setBody("");
        },
        onError: (e) =>
          toast.error("Could not post finding", { description: e.message }),
      },
    );
  };

  return (
    <div className="space-y-2 rounded-md border bg-muted/30 p-2.5">
      <Input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Finding headline"
        maxLength={300}
      />
      <Textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Your read for this workspace…"
        className="min-h-20"
      />
      <Button
        size="sm"
        onClick={submit}
        disabled={!title.trim() || !body.trim() || create.isPending}
      >
        {create.isPending ? (
          <Loader2Icon className="animate-spin" data-icon="inline-start" />
        ) : null}
        Post finding
      </Button>
    </div>
  );
}

function WorkspaceFindings({ workspaceId }: { workspaceId: number }) {
  const posts = usePosts({ workspace_id: workspaceId });

  return (
    <div className="space-y-3">
      <FindingComposer workspaceId={workspaceId} />
      {posts.isPending ? (
        <Skeleton className="h-16 w-full" />
      ) : posts.isError ? (
        <QueryError error={posts.error} onRetry={() => void posts.refetch()} />
      ) : posts.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No findings in this workspace yet.
        </p>
      ) : (
        posts.data.map((post: Post) => (
          <Card key={post.id}>
            <CardContent className="space-y-1 py-3">
              <h4 className="text-sm font-medium">{post.title}</h4>
              <p className="text-sm whitespace-pre-wrap text-muted-foreground">
                {post.body}
              </p>
              {post.document_id ? (
                <Link
                  href={`/documents/${post.document_id}`}
                  className="text-xs text-muted-foreground hover:underline"
                >
                  {post.document_title ?? `document #${post.document_id}`}
                </Link>
              ) : null}
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}

// --- the rail --------------------------------------------------------------

function GroupLabel({ children }: { children: ReactNode }) {
  return (
    <p className="px-1 pt-1 text-[0.7rem] font-semibold tracking-wide text-muted-foreground uppercase">
      {children}
    </p>
  );
}

/**
 * The workspace's supporting context, beside the assistant. Two logical
 * groups — Sources (what feeds the workspace) and Your work (what you produce)
 * — each a collapsible section, so every feature is present but only one or
 * two are ever in your face.
 */
export function WorkspaceContextRail({ workspaceId }: { workspaceId: number }) {
  // counts for the section headers (react-query dedups these with the same
  // keys the panels use, so they don't double-fetch).
  const feed = useWorkspaceFeed(workspaceId, 1);
  const docs = useWorkspaceDocuments(workspaceId, 1);
  const findings = usePosts({ workspace_id: workspaceId });
  const drafts = useWorkspaceSocialDrafts(workspaceId);

  return (
    <div className="space-y-2">
      <GroupLabel>Sources</GroupLabel>
      <CollapsibleSection
        icon={RssIcon}
        title="In focus"
        count={feed.data?.total}
        defaultOpen
      >
        <FocusedFeed workspaceId={workspaceId} />
      </CollapsibleSection>
      <CollapsibleSection
        icon={DatabaseIcon}
        title="Knowledge base"
        count={docs.data?.total}
        hint="Documents you add here join the feed and the assistant's searches."
      >
        <WorkspaceKnowledgeBase workspaceId={workspaceId} />
      </CollapsibleSection>

      <GroupLabel>Your work</GroupLabel>
      <CollapsibleSection
        icon={NotebookPenIcon}
        title="Findings"
        count={findings.data?.length}
      >
        <WorkspaceFindings workspaceId={workspaceId} />
      </CollapsibleSection>
      <CollapsibleSection
        icon={ImageIcon}
        title="Post drafts"
        count={drafts.data?.length}
      >
        <WorkspaceDrafts workspaceId={workspaceId} />
      </CollapsibleSection>
      <CollapsibleSection
        icon={PlayIcon}
        title="Tasks"
        hint="Hand the agent a longer job; it runs in the background and reports back."
      >
        <WorkspaceTasks workspaceId={workspaceId} />
      </CollapsibleSection>
    </div>
  );
}
