"use client";

import { CopyIcon, DownloadIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { SocialDraft } from "@/lib/api";
import { useDeleteSocialDraft, useWorkspaceSocialDrafts } from "@/lib/queries";

function cardUrl(sha: string): string {
  return `/api/social/card/${sha}.jpg`;
}

function captionWithTags(draft: SocialDraft): string {
  const tags = draft.content.hashtags.map((h) => `#${h}`).join(" ");
  return [draft.content.caption, tags].filter(Boolean).join("\n\n");
}

function DraftCard({
  draft,
  onDelete,
  deleting,
}: {
  draft: SocialDraft;
  onDelete: () => void;
  deleting: boolean;
}) {
  return (
    <Card>
      <CardContent className="space-y-2 py-3">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={cardUrl(draft.card_sha)}
          alt={draft.content.alt_text || "Instagram card"}
          className="w-full rounded-md border"
        />
        <p className="text-sm whitespace-pre-wrap">{draft.content.caption}</p>
        {draft.content.hashtags.length > 0 ? (
          <p className="text-xs text-muted-foreground">
            {draft.content.hashtags.map((h) => `#${h}`).join(" ")}
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="xs"
            onClick={() => {
              void navigator.clipboard.writeText(captionWithTags(draft));
              toast.success("Caption copied");
            }}
          >
            <CopyIcon data-icon="inline-start" />
            Copy
          </Button>
          <Button
            variant="outline"
            size="xs"
            render={
              <a
                href={cardUrl(draft.card_sha)}
                download={`post-${draft.id}.jpg`}
              />
            }
          >
            <DownloadIcon data-icon="inline-start" />
            Image
          </Button>
          <Button
            variant="ghost"
            size="xs"
            aria-label="Delete draft"
            disabled={deleting}
            onClick={onDelete}
          >
            <Trash2Icon className="size-3" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function WorkspaceDrafts({ workspaceId }: { workspaceId: number }) {
  const drafts = useWorkspaceSocialDrafts(workspaceId);
  const del = useDeleteSocialDraft(workspaceId);

  if (drafts.isPending) return <Skeleton className="h-40 w-full" />;
  if (drafts.isError || drafts.data.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No post drafts yet. Ask the assistant to “draft an Instagram post” about
        a story in the feed.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {drafts.data.map((draft) => (
        <DraftCard
          key={draft.id}
          draft={draft}
          deleting={del.isPending}
          onDelete={() =>
            del.mutate(draft.id, {
              onSuccess: () => toast.success("Draft deleted"),
              onError: (e) =>
                toast.error("Could not delete", { description: e.message }),
            })
          }
        />
      ))}
    </div>
  );
}
