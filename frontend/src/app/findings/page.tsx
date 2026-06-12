"use client";

import { useState } from "react";
import Link from "next/link";
import {
  FileTextIcon,
  Loader2Icon,
  NotebookPenIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import {
  OwnerByline,
  VisibilityBadge,
  VisibilityToggle,
} from "@/components/shared/Visibility";
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
import type { Post, Visibility } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  useCreatePost,
  useDeletePost,
  useMe,
  usePosts,
} from "@/lib/queries";

function Composer() {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("shared");
  const create = useCreatePost();

  const submit = () => {
    if (!title.trim() || !body.trim()) return;
    create.mutate(
      { title: title.trim(), body: body.trim(), visibility },
      {
        onSuccess: () => {
          toast.success("Finding posted");
          setTitle("");
          setBody("");
          setVisibility("shared");
        },
        onError: (e) =>
          toast.error("Could not post finding", { description: e.message }),
      },
    );
  };

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <NotebookPenIcon className="size-4" />
          Post a finding
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <Input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Headline of your finding"
          maxLength={300}
        />
        <Textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="What did you find in the news? Add your read…"
          className="min-h-28"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <VisibilityToggle value={visibility} onChange={setVisibility} kind="finding" />
        <div className="flex items-center gap-2">
          <Button
            onClick={submit}
            disabled={!title.trim() || !body.trim() || create.isPending}
          >
            {create.isPending ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : null}
            Post finding
          </Button>
          <span className="text-xs text-muted-foreground">⌘/Ctrl + Enter</span>
        </div>
      </CardContent>
    </Card>
  );
}

function PostCard({ post }: { post: Post }) {
  const me = useMe();
  const del = useDeletePost();
  const isOwner = me.data != null && post.owner_id === me.data.id;

  return (
    <Card>
      <CardContent className="space-y-2 py-4">
        <div className="flex items-start justify-between gap-2">
          <h3 className="font-medium">{post.title}</h3>
          {isOwner ? (
            <Button
              variant="ghost"
              size="xs"
              aria-label="Delete finding"
              disabled={del.isPending}
              onClick={() =>
                del.mutate(post.id, {
                  onSuccess: () => toast.success("Finding deleted"),
                  onError: (e) =>
                    toast.error("Could not delete", { description: e.message }),
                })
              }
            >
              <Trash2Icon className="size-4" />
            </Button>
          ) : null}
        </div>
        <p className="text-sm whitespace-pre-wrap">{post.body}</p>
        {post.document_id ? (
          <Link
            href={`/documents/${post.document_id}`}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:underline"
          >
            <FileTextIcon className="size-3" />
            {post.document_title ?? `document #${post.document_id}`}
          </Link>
        ) : null}
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span title={post.created_at}>{relativeTime(post.created_at)}</span>
          <OwnerByline ownerId={post.owner_id} ownerName={post.owner_name} />
          <VisibilityBadge visibility={post.visibility} />
        </div>
      </CardContent>
    </Card>
  );
}

export default function FindingsPage() {
  const posts = usePosts();

  return (
    <>
      <PageHeader
        title="Findings"
        description="Post and share findings from the news."
      />
      <Composer />

      {posts.isPending ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : posts.isError ? (
        <QueryError error={posts.error} onRetry={() => void posts.refetch()} />
      ) : posts.data.length === 0 ? (
        <EmptyState
          icon={NotebookPenIcon}
          title="No findings yet"
          description="Post the first finding from the news above."
        />
      ) : (
        <div className="space-y-3">
          {posts.data.map((post) => (
            <PostCard key={post.id} post={post} />
          ))}
        </div>
      )}
    </>
  );
}
