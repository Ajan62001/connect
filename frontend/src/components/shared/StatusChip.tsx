import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { EnrichmentStatus } from "@/lib/api";

const STATUS_STYLES: Record<EnrichmentStatus, { label: string; className: string }> = {
  pending: {
    label: "pending",
    className: "bg-muted text-muted-foreground",
  },
  queued: {
    label: "queued",
    className:
      "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  },
  done: {
    label: "done",
    className:
      "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  },
  failed: {
    label: "failed",
    className: "bg-destructive/10 text-destructive dark:bg-destructive/20",
  },
  skipped_dup: {
    label: "duplicate",
    className:
      "border-border bg-transparent text-muted-foreground line-through decoration-muted-foreground/50",
  },
  skipped_aged: {
    label: "aged out",
    className: "border-border bg-transparent text-muted-foreground",
  },
};

export function StatusChip({
  status,
  className,
}: {
  status: EnrichmentStatus;
  className?: string;
}) {
  const style = STATUS_STYLES[status] ?? {
    label: status,
    className: "bg-muted text-muted-foreground",
  };
  return (
    <Badge variant="secondary" className={cn(style.className, className)}>
      {style.label}
    </Badge>
  );
}

export function WatchHitChip({ className }: { className?: string }) {
  return (
    <Badge
      variant="secondary"
      className={cn(
        "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
        className,
      )}
    >
      watch hit
    </Badge>
  );
}
