"use client";

import { useRouter } from "next/navigation";
import { Loader2Icon, TelescopeIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import type { InvestigationCreate } from "@/lib/api";
import { useCreateInvestigation } from "@/lib/queries";

/**
 * "Investigate" entry point on entity/event/thread pages: seeds an
 * investigation with the page's object id and navigates to the live run.
 */
export function InvestigateButton({
  seed,
  variant = "outline",
  size = "sm",
}: {
  seed: Pick<
    InvestigationCreate,
    "topic" | "entity_id" | "event_id" | "story_id"
  >;
  variant?: "default" | "outline" | "ghost";
  size?: "default" | "sm" | "xs";
}) {
  const router = useRouter();
  const create = useCreateInvestigation();

  return (
    <Button
      variant={variant}
      size={size}
      disabled={create.isPending}
      onClick={() =>
        create.mutate(seed, {
          onSuccess: (accepted) =>
            router.push(`/investigation/${accepted.investigation_id}`),
          onError: (error) =>
            toast.error("Could not start investigation", {
              description: error.message,
            }),
        })
      }
    >
      {create.isPending ? (
        <Loader2Icon className="animate-spin" data-icon="inline-start" />
      ) : (
        <TelescopeIcon data-icon="inline-start" />
      )}
      Investigate
    </Button>
  );
}
