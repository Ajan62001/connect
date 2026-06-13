"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeftIcon,
  DatabaseIcon,
  ImageIcon,
  LayoutGridIcon,
  Loader2Icon,
  NotebookPenIcon,
  PlayIcon,
  RssIcon,
  SearchIcon,
  Settings2Icon,
  SparklesIcon,
  TagIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { VisibilityBadge } from "@/components/shared/Visibility";
import { TellStoryButton } from "@/components/story/TellStoryButton";
import { PostSettingsDialog } from "@/components/workspace/PostSettingsDialog";
import { WorkspaceSourcesDialog } from "@/components/workspace/WorkspaceSourcesDialog";
import { WorkspaceChatPanel } from "@/components/workspace/WorkspaceChatPanel";
import { WorkspaceDrafts } from "@/components/workspace/WorkspaceDrafts";
import { WorkspaceKnowledgeBase } from "@/components/workspace/WorkspaceKnowledgeBase";
import { WorkspaceTasks } from "@/components/workspace/WorkspaceTasks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type { Post } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  useCreatePost,
  useDeleteWorkspace,
  usePosts,
  useWorkspace,
  useWorkspaceFeed,
} from "@/lib/queries";

function FocusedFeed({ workspaceId }: { workspaceId: number }) {
  const [page, setPage] = useState(1);
  const feed = useWorkspaceFeed(workspaceId, page);

  if (feed.isPending) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
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
        <Card key={doc.id}>
          <CardContent className="py-3">
            <Link
              href={`/documents/${doc.id}`}
              className="font-medium hover:underline"
            >
              {doc.title ?? "(untitled document)"}
            </Link>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{doc.source_name ?? "Manual ingest"}</span>
              <span title={doc.fetched_at}>{relativeTime(doc.fetched_at)}</span>
            </div>
          </CardContent>
        </Card>
      ))}
      <div className="flex items-center justify-between pt-1 text-sm text-muted-foreground">
        <span>{total} in focus</span>
        <span className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={cur <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            Previous
          </Button>
          <Button
            variant="outline"
            size="sm"
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
    <div className="space-y-2 rounded-lg border bg-muted/30 p-3">
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

export default function WorkspacePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const workspaceId = Number(id);
  const workspace = useWorkspace(workspaceId);
  const del = useDeleteWorkspace();
  const router = useRouter();
  const [postSettingsOpen, setPostSettingsOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);

  if (!Number.isFinite(workspaceId)) {
    return (
      <EmptyState
        icon={LayoutGridIcon}
        title="Invalid workspace"
        description={`“${id}” is not a workspace id.`}
      />
    );
  }

  return (
    <>
      <Button variant="ghost" size="sm" render={<Link href="/workspaces" />}>
        <ArrowLeftIcon data-icon="inline-start" />
        Workspaces
      </Button>

      {workspace.isPending ? (
        <div className="mt-4 space-y-3">
          <Skeleton className="h-10 w-64" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : workspace.isError ? (
        <QueryError
          error={workspace.error}
          onRetry={() => void workspace.refetch()}
        />
      ) : (
        <>
          <PageHeader
            title={workspace.data.name}
            description={workspace.data.description || undefined}
            actions={
              <span className="flex gap-2">
                <TellStoryButton source={{ workspace_id: workspaceId }} />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setSourcesOpen(true)}
                >
                  <DatabaseIcon data-icon="inline-start" />
                  Sources
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPostSettingsOpen(true)}
                >
                  <Settings2Icon data-icon="inline-start" />
                  Post settings
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    del.mutate(workspaceId, {
                      onSuccess: () => {
                        toast.success("Workspace deleted");
                        router.push("/workspaces");
                      },
                      onError: (e) =>
                        toast.error("Could not delete", {
                          description: e.message,
                        }),
                    })
                  }
                >
                  <Trash2Icon data-icon="inline-start" />
                  Delete
                </Button>
              </span>
            }
          />
          <PostSettingsDialog
            workspaceId={workspaceId}
            open={postSettingsOpen}
            onOpenChange={setPostSettingsOpen}
          />
          <WorkspaceSourcesDialog
            workspaceId={workspaceId}
            open={sourcesOpen}
            onOpenChange={setSourcesOpen}
          />

          <div className="mb-4 flex flex-wrap items-center gap-1.5">
            {workspace.data.topics.map((t) => (
              <Badge key={t} variant="secondary">
                <TagIcon className="size-3" />
                {t}
              </Badge>
            ))}
            {workspace.data.query_fts ? (
              <Badge variant="outline">
                <SearchIcon className="size-3" />
                {workspace.data.query_fts}
              </Badge>
            ) : null}
            <VisibilityBadge visibility={workspace.data.visibility} />
          </div>

          <div className="flex flex-col gap-6 lg:flex-row">
            <section className="min-w-0 flex-1 space-y-6">
              <div>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  <RssIcon className="size-4" />
                  Focused feed
                </h2>
                <FocusedFeed workspaceId={workspaceId} />
              </div>
              <div>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  <DatabaseIcon className="size-4" />
                  Knowledge base
                </h2>
                <WorkspaceKnowledgeBase workspaceId={workspaceId} />
              </div>
            </section>
            <section className="w-full shrink-0 space-y-6 lg:w-96">
              <div>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  <SparklesIcon className="size-4" />
                  Assistant
                </h2>
                <WorkspaceChatPanel workspaceId={workspaceId} />
              </div>
              <div>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  <PlayIcon className="size-4" />
                  Tasks
                </h2>
                <WorkspaceTasks workspaceId={workspaceId} />
              </div>
              <div>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  <ImageIcon className="size-4" />
                  Post drafts
                </h2>
                <WorkspaceDrafts workspaceId={workspaceId} />
              </div>
              <div>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  <NotebookPenIcon className="size-4" />
                  Findings
                </h2>
                <WorkspaceFindings workspaceId={workspaceId} />
              </div>
            </section>
          </div>
        </>
      )}
    </>
  );
}
