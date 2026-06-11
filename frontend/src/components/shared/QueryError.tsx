"use client";

import { CircleAlertIcon, RotateCcwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

export function QueryError({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-destructive/30 bg-destructive/5 px-6 py-12 text-center">
      <CircleAlertIcon className="size-6 text-destructive" aria-hidden />
      <div className="space-y-1">
        <p className="text-sm font-medium text-destructive">
          Something went wrong
        </p>
        <p className="max-w-md text-sm text-muted-foreground">{error.message}</p>
      </div>
      {onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RotateCcwIcon data-icon="inline-start" />
          Retry
        </Button>
      ) : null}
    </div>
  );
}
