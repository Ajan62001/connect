"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, PlusIcon, XIcon } from "lucide-react";
import { toast } from "sonner";

import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useUpdateWorkspace,
  useWorkspaceSourceSuggestions,
} from "@/lib/queries";

export function WorkspaceSourcesDialog({
  workspaceId,
  open,
  onOpenChange,
}: {
  workspaceId: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const suggestions = useWorkspaceSourceSuggestions(workspaceId);
  const update = useUpdateWorkspace(workspaceId);

  const current = suggestions.data?.current ?? [];
  const currentIds = current.map((s) => s.id);

  const apply = (sourceIds: number[]) =>
    update.mutate(
      { source_ids: sourceIds },
      {
        onSuccess: () => {
          void queryClient.invalidateQueries({
            queryKey: ["workspaces", "source-suggestions", workspaceId],
          });
          void queryClient.invalidateQueries({
            queryKey: ["workspaces", "feed", workspaceId],
          });
        },
        onError: (e) =>
          toast.error("Could not update sources", { description: e.message }),
      },
    );

  const add = (id: number) => apply([...currentIds, id]);
  const remove = (id: number) => apply(currentIds.filter((s) => s !== id));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Sources in focus</DialogTitle>
          <DialogDescription>
            Add the sources this workspace should follow. Suggestions are drawn
            from the sources publishing on this workspace&apos;s topics.
          </DialogDescription>
        </DialogHeader>

        {suggestions.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : suggestions.isError ? (
          <QueryError
            error={suggestions.error}
            onRetry={() => void suggestions.refetch()}
          />
        ) : (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                In focus
              </p>
              {current.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No sources added — the workspace follows its topics and query
                  across the whole corpus.
                </p>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {current.map((s) => (
                    <Badge key={s.id} variant="secondary" className="gap-1">
                      {s.name}
                      <button
                        type="button"
                        onClick={() => remove(s.id)}
                        disabled={update.isPending}
                        aria-label={`Remove ${s.name}`}
                        className="hover:text-destructive"
                      >
                        <XIcon className="size-3" />
                      </button>
                    </Badge>
                  ))}
                </div>
              )}
            </div>

            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                Suggested from your topics
              </p>
              {suggestions.data.suggestions.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No source suggestions yet — add focus topics to this
                  workspace, or enrich more documents so their topics are known.
                </p>
              ) : (
                <ul className="divide-y rounded-lg border">
                  {suggestions.data.suggestions.map((s) => (
                    <li
                      key={s.id}
                      className="flex items-center gap-2 px-3 py-2 text-sm"
                    >
                      <span className="min-w-0 flex-1 truncate">{s.name}</span>
                      <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                        {s.doc_count} doc{s.doc_count === 1 ? "" : "s"}
                      </span>
                      <Button
                        size="xs"
                        variant="outline"
                        disabled={update.isPending}
                        onClick={() => add(s.id)}
                      >
                        {update.isPending ? (
                          <Loader2Icon
                            className="animate-spin"
                            data-icon="inline-start"
                          />
                        ) : (
                          <PlusIcon data-icon="inline-start" />
                        )}
                        Add
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
