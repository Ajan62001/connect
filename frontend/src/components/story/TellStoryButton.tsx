"use client";

import { useRouter } from "next/navigation";
import { BookOpenIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import type { StoryCreateInput } from "@/lib/api";
import { useCreateStory } from "@/lib/queries";

/** Kick off a grounded story from a source (thread / investigation /
 *  workspace) and navigate to it. */
export function TellStoryButton({
  source,
  label = "Tell the story",
  variant = "outline",
  size = "sm",
}: {
  source: StoryCreateInput;
  label?: string;
  variant?: React.ComponentProps<typeof Button>["variant"];
  size?: React.ComponentProps<typeof Button>["size"];
}) {
  const router = useRouter();
  const create = useCreateStory();
  return (
    <Button
      variant={variant}
      size={size}
      disabled={create.isPending}
      onClick={() =>
        create.mutate(source, {
          onSuccess: (a) => router.push(`/story/${a.story_id}`),
          onError: (e) =>
            toast.error("Could not start the story", { description: e.message }),
        })
      }
    >
      {create.isPending ? (
        <Loader2Icon className="animate-spin" data-icon="inline-start" />
      ) : (
        <BookOpenIcon data-icon="inline-start" />
      )}
      {label}
    </Button>
  );
}
