"use client";

import { use } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowLeftIcon,
  FilmIcon,
  LayoutGridIcon,
  SearchIcon,
  Settings2Icon,
  SparklesIcon,
  TagIcon,
} from "lucide-react";

import { ContentStudio } from "@/components/content/ContentStudio";
import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { VisibilityBadge } from "@/components/shared/Visibility";
import { TellStoryButton } from "@/components/story/TellStoryButton";
import { ChannelSettingsPanel } from "@/components/workspace/settings/ChannelSettingsPanel";
import { WorkspaceChatPanel } from "@/components/workspace/WorkspaceChatPanel";
import { WorkspaceContextRail } from "@/components/workspace/WorkspaceContextRail";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useWorkspace } from "@/lib/queries";

const TABS = ["studio", "chat", "settings"] as const;
type Tab = (typeof TABS)[number];

export default function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const workspaceId = Number(id);
  const workspace = useWorkspace(workspaceId);
  const router = useRouter();
  const searchParams = useSearchParams();

  const tabParam = searchParams.get("tab");
  const tab: Tab = TABS.includes(tabParam as Tab) ? (tabParam as Tab) : "studio";
  const setTab = (next: string) => {
    const sp = new URLSearchParams(searchParams);
    sp.set("tab", next);
    router.replace(`?${sp.toString()}`, { scroll: false });
  };

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
          <Skeleton className="h-[60vh] w-full" />
        </div>
      ) : workspace.isError ? (
        <QueryError error={workspace.error} onRetry={() => void workspace.refetch()} />
      ) : (
        <>
          <PageHeader
            title={workspace.data.name}
            description={workspace.data.description || undefined}
            actions={<TellStoryButton source={{ workspace_id: workspaceId }} />}
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

          <Tabs value={tab} onValueChange={setTab}>
            <TabsList>
              <TabsTrigger value="studio">
                <FilmIcon data-icon="inline-start" />
                Studio
              </TabsTrigger>
              <TabsTrigger value="chat">
                <SparklesIcon data-icon="inline-start" />
                Chat
              </TabsTrigger>
              <TabsTrigger value="settings">
                <Settings2Icon data-icon="inline-start" />
                Settings
              </TabsTrigger>
            </TabsList>

            <TabsContent value="studio" className="pt-4">
              <ContentStudio workspaceId={workspaceId} />
            </TabsContent>

            <TabsContent value="chat" className="pt-4">
              <p className="mb-4 flex items-center gap-1.5 text-sm text-muted-foreground">
                <SparklesIcon className="size-3.5 shrink-0" />A focused lens over the news — the
                assistant searches and acts only over the documents in focus.
              </p>
              <div className="flex flex-col gap-6 lg:flex-row">
                <section className="min-w-0 flex-1">
                  <WorkspaceChatPanel workspaceId={workspaceId} hero />
                </section>
                <aside className="w-full shrink-0 lg:w-[380px]">
                  <WorkspaceContextRail workspaceId={workspaceId} />
                </aside>
              </div>
            </TabsContent>

            <TabsContent value="settings" className="pt-4">
              <ChannelSettingsPanel workspaceId={workspaceId} workspace={workspace.data} />
            </TabsContent>
          </Tabs>
        </>
      )}
    </>
  );
}
