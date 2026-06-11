import { cn } from "@/lib/utils";
import type { Source } from "@/lib/api";
import { absoluteTime, relativeTime } from "@/lib/format";

/**
 * Poll-health chip: green when the last poll reported ok, amber when it was
 * deliberately skipped (e.g. twitter without TWITTERAPI_IO_API_KEY — by
 * design, not a failure), red otherwise, gray when never polled.
 */
export function SourceHealthBadge({ source }: { source: Source }) {
  const { last_polled_at, last_poll_status } = source;

  if (!last_polled_at && !last_poll_status) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <span className="size-2 rounded-full bg-muted-foreground/30" aria-hidden />
        never polled
      </span>
    );
  }

  const status = (last_poll_status ?? "").toLowerCase();
  const healthy = status.startsWith("ok");
  const skipped = status.startsWith("skipped");
  return (
    <span
      className="inline-flex max-w-48 items-center gap-1.5 text-xs"
      title={`${last_poll_status ?? "unknown"} · ${absoluteTime(last_polled_at)}`}
    >
      <span
        className={cn(
          "size-2 shrink-0 rounded-full",
          healthy
            ? "bg-emerald-500"
            : skipped
              ? "bg-amber-500"
              : "bg-destructive",
        )}
        aria-hidden
      />
      <span
        className={cn(
          "truncate",
          healthy || skipped ? "text-muted-foreground" : "text-destructive",
        )}
      >
        {healthy ? "ok" : (last_poll_status ?? "unknown")}
        {last_polled_at ? ` · ${relativeTime(last_polled_at)}` : ""}
      </span>
    </span>
  );
}
