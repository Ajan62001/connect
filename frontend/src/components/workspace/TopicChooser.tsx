"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PlusIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { getTopics } from "@/lib/api";

/**
 * Pick a workspace's focus topics. The controlled T1 vocabulary is offered as
 * one-tap chips (so common picks line up with the auto-theme topic palettes),
 * but the user can also add any custom free-text topic — selected topics that
 * fall outside the vocabulary are shown as their own removable chips so they
 * stay visible and editable.
 */
export function TopicChooser({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const topics = useQuery({
    queryKey: ["topics"],
    queryFn: getTopics,
    staleTime: Infinity,
  });
  const [custom, setCustom] = useState("");

  const toggle = (t: string) =>
    onChange(value.includes(t) ? value.filter((x) => x !== t) : [...value, t]);

  const addCustom = () => {
    const t = custom.trim();
    if (t && !value.includes(t)) onChange([...value, t]);
    setCustom("");
  };

  const vocab = topics.data ?? [];
  // Selected topics outside the vocabulary (custom free-text) — render them so
  // existing ones are visible and removable, not silently dropped from the UI.
  const customSelected = value.filter((t) => !vocab.includes(t));

  return (
    <div className="space-y-2">
      {customSelected.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {customSelected.map((t) => (
            <Button
              key={t}
              type="button"
              size="xs"
              variant="secondary"
              aria-pressed
              title="Remove topic"
              onClick={() => toggle(t)}
            >
              {t}
              <XIcon data-icon="inline-end" />
            </Button>
          ))}
        </div>
      ) : null}

      {topics.isPending ? (
        <Skeleton className="h-16 w-full" />
      ) : topics.isError ? (
        <p className="text-xs text-muted-foreground">
          Couldn’t load suggested topics — you can still add custom ones below.
        </p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {vocab.map((t) => (
            <Button
              key={t}
              type="button"
              size="xs"
              variant={value.includes(t) ? "secondary" : "outline"}
              aria-pressed={value.includes(t)}
              onClick={() => toggle(t)}
            >
              {t}
            </Button>
          ))}
        </div>
      )}

      <div className="flex gap-1.5">
        <Input
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          placeholder="Add a custom topic…"
          className="h-7 text-xs"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addCustom();
            }
          }}
        />
        <Button
          type="button"
          size="xs"
          variant="outline"
          disabled={!custom.trim()}
          onClick={addCustom}
        >
          <PlusIcon data-icon="inline-start" />
          Add
        </Button>
      </div>
    </div>
  );
}
