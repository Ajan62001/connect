"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { BookOpenIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { AnalysisStatusChip } from "@/components/analysis/VerdictBadge";
import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { StoryLength, StoryTone } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useCreateStory, useStories } from "@/lib/queries";
import { cn } from "@/lib/utils";

const LENGTHS: { value: StoryLength; label: string }[] = [
  { value: "brief", label: "Brief" },
  { value: "standard", label: "Standard" },
  { value: "feature", label: "Feature" },
];
const STYLE_PRESETS = [
  "Tight newswire",
  "Narrative feature",
  "Explainer for a general audience",
  "Analytical brief",
] as const;

const TONES: { value: StoryTone; label: string }[] = [
  { value: "explanatory", label: "Explanatory" },
  { value: "neutral", label: "Neutral" },
];

function Toggle<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex rounded-lg border p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
            value === o.value
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function RecentStories() {
  const stories = useStories({ limit: 20 });
  if (stories.isPending) {
    return (
      <div className="space-y-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-14 w-full rounded-lg" />
        ))}
      </div>
    );
  }
  if (stories.isError) {
    return (
      <QueryError error={stories.error} onRetry={() => void stories.refetch()} />
    );
  }
  if (stories.data.items.length === 0) {
    return (
      <EmptyState
        icon={BookOpenIcon}
        title="No stories yet"
        description="Give a topic above, or use “Tell the story” on a thread, investigation, or workspace."
      />
    );
  }
  return (
    <ul className="divide-y rounded-xl border bg-card">
      {stories.data.items.map((s) => (
        <li key={s.id}>
          <Link
            href={`/story/${s.id}`}
            className="flex flex-col gap-1.5 px-3 py-3 transition-colors hover:bg-muted/60"
          >
            <p className="line-clamp-2 text-sm font-medium">
              {s.title ?? s.subject}
            </p>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <AnalysisStatusChip status={s.status} />
              <Badge variant="outline">{s.input_type}</Badge>
              <span className="ml-auto">{relativeTime(s.created_at)}</span>
            </div>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export default function StoriesPage() {
  const router = useRouter();
  const create = useCreateStory();
  const [topic, setTopic] = useState("");
  const [length, setLength] = useState<StoryLength>("standard");
  const [tone, setTone] = useState<StoryTone>("explanatory");
  const [style, setStyle] = useState("");

  const canSubmit = topic.trim().length > 0 && !create.isPending;
  const submit = () => {
    if (!canSubmit) return;
    create.mutate(
      {
        topic: topic.trim(),
        options: { length, tone, style: style.trim() || undefined },
      },
      {
        onSuccess: (a) => router.push(`/story/${a.story_id}`),
        onError: (e) =>
          toast.error("Could not start the story", { description: e.message }),
      },
    );
  };

  return (
    <div className="mx-auto max-w-3xl space-y-8 py-6">
      <Card>
        <CardHeader className="text-center">
          <CardTitle className="text-lg">Tell a story</CardTitle>
          <CardDescription>
            A grounded narrative — what happened, what it was, why it happened,
            and how it affects things — written strictly from the stored facts,
            with every claim cited.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="A topic to narrate from the corpus…"
            className="min-h-24"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="space-y-1.5">
            <Input
              value={style}
              onChange={(e) => setStyle(e.target.value)}
              placeholder="Style (optional) — e.g. “tight newswire”, “narrative feature for a general audience”"
              maxLength={200}
            />
            <div className="flex flex-wrap gap-1.5">
              {STYLE_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  onClick={() => setStyle(preset)}
                  className="rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                >
                  {preset}
                </button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-3">
              <Toggle options={LENGTHS} value={length} onChange={setLength} />
              <Toggle options={TONES} value={tone} onChange={setTone} />
            </div>
            <Button disabled={!canSubmit} onClick={submit}>
              {create.isPending ? (
                <Loader2Icon className="animate-spin" data-icon="inline-start" />
              ) : (
                <BookOpenIcon data-icon="inline-start" />
              )}
              Tell the story
            </Button>
          </div>
        </CardContent>
      </Card>

      <section className="space-y-3">
        <h2 className="text-sm font-medium text-muted-foreground">
          Recent stories
        </h2>
        <RecentStories />
      </section>
    </div>
  );
}
