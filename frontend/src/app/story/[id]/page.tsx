"use client";

import { use, useState } from "react";
import Link from "next/link";
import {
  ArrowLeftIcon,
  BookOpenIcon,
  CheckIcon,
  Loader2Icon,
  PencilIcon,
  XIcon,
} from "lucide-react";
import { toast } from "sonner";

import { AnalysisStatusChip } from "@/components/analysis/VerdictBadge";
import { InvestigateButton } from "@/components/investigation/InvestigateButton";
import { AskBox } from "@/components/shared/AskBox";
import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import {
  StoryNarrative,
  buildSourcesByRef,
} from "@/components/story/StoryNarrative";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { askStory } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useCancelStory, useEditStory, useStory } from "@/lib/queries";

export default function StoryPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const storyId = Number(id);
  const { query, activity, isLive } = useStory(storyId);
  const cancel = useCancelStory(storyId);
  const edit = useEditStory(storyId);
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftBody, setDraftBody] = useState("");

  if (query.isPending) {
    return (
      <div className="mx-auto max-w-3xl space-y-4 py-6">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="mx-auto max-w-3xl py-6">
        <QueryError error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const story = query.data;
  const sources = buildSourcesByRef(story.sources);
  const grounding = story.grounding;
  const completed = story.status === "completed";

  const startEdit = () => {
    setDraftTitle(story.title ?? "");
    setDraftBody(story.narrative_md ?? "");
    setEditing(true);
  };
  const save = () =>
    edit.mutate(
      { title: draftTitle.trim() || undefined, narrative_md: draftBody },
      {
        onSuccess: () => setEditing(false),
        onError: (e) =>
          toast.error("Could not save", { description: e.message }),
      },
    );

  return (
    <div className="mx-auto max-w-3xl space-y-6 py-6">
      <div>
        <Link
          href="/stories"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeftIcon className="size-3" /> Stories
        </Link>
        <div className="mt-2 flex items-start justify-between gap-3">
          <h1 className="text-xl font-semibold tracking-tight">
            {story.title ?? story.subject}
          </h1>
          {isLive ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
            >
              <XIcon data-icon="inline-start" /> Cancel
            </Button>
          ) : completed && !editing ? (
            <div className="flex shrink-0 gap-2">
              <Button variant="outline" size="sm" onClick={startEdit}>
                <PencilIcon data-icon="inline-start" /> Edit
              </Button>
              {story.input_type !== "investigation" ? (
                <InvestigateButton seed={{ topic: story.subject }} />
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <AnalysisStatusChip status={story.status} />
          <Badge variant="outline">{story.input_type}</Badge>
          <span>from “{story.subject}”</span>
          {grounding?.edited ? <Badge variant="secondary">edited</Badge> : null}
          <span className="ml-auto">started {relativeTime(story.created_at)}</span>
        </div>
      </div>

      {isLive ? (
        <Card>
          <CardContent className="space-y-2 py-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Loader2Icon className="size-4 animate-spin text-muted-foreground" />
              Writing the story…
            </div>
            {activity.length > 0 ? (
              <ul className="space-y-0.5 text-xs text-muted-foreground">
                {activity.map((line, i) => (
                  <li key={i}>· {line}</li>
                ))}
              </ul>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {story.status === "failed" ? (
        <Card>
          <CardContent className="py-4 text-sm text-destructive">
            {story.error ?? "The story could not be written."}
          </CardContent>
        </Card>
      ) : null}

      {editing ? (
        <Card>
          <CardContent className="space-y-2 py-4">
            <Input
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              placeholder="Title"
            />
            <Textarea
              value={draftBody}
              onChange={(e) => setDraftBody(e.target.value)}
              className="min-h-72 font-mono text-xs"
              placeholder="Story markdown… (citation markers like [[E1]] link to sources)"
            />
            <div className="flex items-center gap-2">
              <Button size="sm" disabled={edit.isPending} onClick={save}>
                {edit.isPending ? (
                  <Loader2Icon className="animate-spin" data-icon="inline-start" />
                ) : (
                  <CheckIcon data-icon="inline-start" />
                )}
                Save
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setEditing(false)}
              >
                Cancel
              </Button>
              <span className="text-xs text-muted-foreground">
                Edits are your own — they aren&apos;t re-verified against
                sources.
              </span>
            </div>
          </CardContent>
        </Card>
      ) : story.narrative_md ? (
        <StoryNarrative markdown={story.narrative_md} sources={sources} />
      ) : completed ? (
        <EmptyState
          icon={BookOpenIcon}
          title="No narrative"
          description="The sources didn't support a story."
        />
      ) : null}

      {grounding && completed && !editing ? (
        <p className="text-xs text-muted-foreground">
          Grounded in {grounding.cited_count} of {grounding.menu_size} fact
          {grounding.menu_size === 1 ? "" : "s"}
          {grounding.stripped_markers.length > 0
            ? ` · ${grounding.stripped_markers.length} unverifiable citation(s) removed`
            : ""}
          {grounding.regenerated ? " · regenerated once for grounding" : ""}
          {grounding.edited ? " · hand-edited after generation" : ""}.
        </p>
      ) : null}

      {completed && story.narrative_md && !editing ? (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold tracking-tight">
            Cross-question
          </h2>
          <AskBox
            onAsk={(q) => askStory(storyId, q)}
            placeholder="Ask a follow-up about this story…"
          />
        </section>
      ) : null}

      {story.sources.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold tracking-tight">Sources</h2>
          <ul className="divide-y rounded-xl border bg-card">
            {story.sources.map((s) => (
              <li key={s.ref} className="flex gap-2 px-3 py-2 text-sm">
                <span className="mt-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded bg-muted px-1 text-[10px] font-medium tabular-nums text-muted-foreground">
                  {s.ref.replace(/^E/, "")}
                </span>
                <div className="min-w-0 flex-1">
                  {s.document_id != null ? (
                    <Link
                      href={`/documents/${s.document_id}`}
                      className="font-medium hover:underline"
                    >
                      {s.title ?? `Document #${s.document_id}`}
                    </Link>
                  ) : (
                    <span className="font-medium">{s.title ?? "Source"}</span>
                  )}
                  <div className="text-xs text-muted-foreground">
                    {s.source_name ?? "unknown source"}
                    {s.occurred_on ? ` · ${s.occurred_on}` : ""}
                  </div>
                  {s.quote ? (
                    <p className="mt-1 line-clamp-2 text-xs italic text-muted-foreground">
                      “{s.quote}”
                    </p>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
