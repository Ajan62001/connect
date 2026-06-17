"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeftIcon,
  DatabaseIcon,
  FilmIcon,
  LayoutGridIcon,
  Loader2Icon,
  PencilIcon,
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
import { WorkspaceContentSheet } from "@/components/workspace/WorkspaceContentSheet";
import { TopicChooser } from "@/components/workspace/TopicChooser";
import { WorkspaceChatPanel } from "@/components/workspace/WorkspaceChatPanel";
import { WorkspaceContextRail } from "@/components/workspace/WorkspaceContextRail";
import { WorkspaceSourcesDialog } from "@/components/workspace/WorkspaceSourcesDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  useDeleteWorkspace,
  useUpdateWorkspace,
  useWorkspace,
} from "@/lib/queries";

export default function WorkspacePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const workspaceId = Number(id);
  const workspace = useWorkspace(workspaceId);
  const del = useDeleteWorkspace();
  const updateWs = useUpdateWorkspace(workspaceId);
  const router = useRouter();
  const [postSettingsOpen, setPostSettingsOpen] = useState(false);
  const [contentOpen, setContentOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [editTopics, setEditTopics] = useState(false);
  const [draftTopics, setDraftTopics] = useState<string[]>([]);

  if (!Number.isFinite(workspaceId)) {
    return (
      <EmptyState
        icon={LayoutGridIcon}
        title="Invalid workspace"
        description={`“${id}” is not a workspace id.`}
      />
    );
  }

  const remove = () =>
    del.mutate(workspaceId, {
      onSuccess: () => {
        toast.success("Workspace deleted");
        router.push("/workspaces");
      },
      onError: (e) =>
        toast.error("Could not delete", { description: e.message }),
    });

  return (
    <>
      <Button variant="ghost" size="sm" render={<Link href="/workspaces" />}>
        <ArrowLeftIcon data-icon="inline-start" />
        Workspaces
      </Button>

      {workspace.isPending ? (
        <div className="mt-4 space-y-3">
          <Skeleton className="h-10 w-64" />
          <Skeleton className="h-[60vh] w-full" />
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
              <div className="flex items-center gap-1.5">
                <TellStoryButton source={{ workspace_id: workspaceId }} />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setContentOpen(true)}
                >
                  <FilmIcon data-icon="inline-start" />
                  Content
                </Button>
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
                  Post style
                </Button>
                <Separator orientation="vertical" className="mx-0.5 h-5" />
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label="Delete workspace"
                        disabled={del.isPending}
                        onClick={remove}
                      />
                    }
                  >
                    <Trash2Icon />
                  </TooltipTrigger>
                  <TooltipContent>Delete workspace</TooltipContent>
                </Tooltip>
              </div>
            }
          />

          <PostSettingsDialog
            workspaceId={workspaceId}
            open={postSettingsOpen}
            onOpenChange={setPostSettingsOpen}
          />
          <WorkspaceContentSheet
            workspaceId={workspaceId}
            name={workspace.data.name}
            open={contentOpen}
            onOpenChange={setContentOpen}
          />
          <WorkspaceSourcesDialog
            workspaceId={workspaceId}
            open={sourcesOpen}
            onOpenChange={setSourcesOpen}
          />

          {editTopics ? (
            <div className="mb-3 space-y-2 rounded-lg border bg-muted/30 p-3">
              <p className="text-xs font-medium text-muted-foreground">
                Focus topics — pick any this workspace should track
              </p>
              <TopicChooser value={draftTopics} onChange={setDraftTopics} />
              <div className="flex gap-2 pt-1">
                <Button
                  size="sm"
                  disabled={updateWs.isPending}
                  onClick={() =>
                    updateWs.mutate(
                      { topics: draftTopics },
                      {
                        onSuccess: () => {
                          toast.success("Focus topics updated");
                          setEditTopics(false);
                        },
                        onError: (e) =>
                          toast.error("Could not update", {
                            description: e.message,
                          }),
                      },
                    )
                  }
                >
                  {updateWs.isPending ? (
                    <Loader2Icon className="animate-spin" data-icon="inline-start" />
                  ) : null}
                  Save topics
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setEditTopics(false)}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="mb-3 flex flex-wrap items-center gap-1.5">
              {workspace.data.topics.length > 0 ? (
                workspace.data.topics.map((t) => (
                  <Badge key={t} variant="secondary">
                    <TagIcon className="size-3" />
                    {t}
                  </Badge>
                ))
              ) : (
                <span className="text-xs text-muted-foreground">
                  No focus topics
                </span>
              )}
              {workspace.data.query_fts ? (
                <Badge variant="outline">
                  <SearchIcon className="size-3" />
                  {workspace.data.query_fts}
                </Badge>
              ) : null}
              <VisibilityBadge visibility={workspace.data.visibility} />
              <Button
                size="xs"
                variant="ghost"
                onClick={() => {
                  setDraftTopics(workspace.data.topics);
                  setEditTopics(true);
                }}
              >
                <PencilIcon data-icon="inline-start" />
                Edit topics
              </Button>
            </div>
          )}

          <p className="mb-5 flex items-center gap-1.5 text-sm text-muted-foreground">
            <SparklesIcon className="size-3.5 shrink-0" />
            A focused lens over the news — the assistant searches and acts only
            over the documents in focus. Everything else lives in the rail.
          </p>

          <div className="flex flex-col gap-6 lg:flex-row">
            <section className="min-w-0 flex-1">
              <WorkspaceChatPanel workspaceId={workspaceId} hero />
            </section>
            <aside className="w-full shrink-0 lg:w-[380px]">
              <WorkspaceContextRail workspaceId={workspaceId} />
            </aside>
          </div>
        </>
      )}
    </>
  );
}
