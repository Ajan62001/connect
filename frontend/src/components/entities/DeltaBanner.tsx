"use client";

import { useEffect, useState } from "react";
import { SparklesIcon } from "lucide-react";

import type { EntityDelta } from "@/lib/api";
import { usePostCursor } from "@/lib/queries";

/**
 * "Since you last looked: +N events, +N documents, +N claims."
 *
 * Mounting the surface is what "looking" means, so the component upserts the
 * view cursor once on mount. The delta it displays was computed against the
 * previous cursor and is frozen in local state, so a background refetch
 * (which would now report zeros) cannot blank the banner mid-visit.
 */
export function DeltaBanner({
  surface,
  refId,
  delta: initialDelta,
}: {
  surface: string;
  refId: number;
  delta: EntityDelta | null;
}) {
  const [delta] = useState(initialDelta);
  const { mutate: postCursor } = usePostCursor();

  useEffect(() => {
    // Fire-and-forget; the upsert is idempotent, so a StrictMode double
    // effect or a failed POST (cursor just stays older) is harmless.
    postCursor({ surface, ref_id: refId });
  }, [postCursor, surface, refId]);

  if (!delta) return null;
  const bits: string[] = [];
  if (delta.events > 0)
    bits.push(`+${delta.events} event${delta.events === 1 ? "" : "s"}`);
  if (delta.documents > 0)
    bits.push(`+${delta.documents} document${delta.documents === 1 ? "" : "s"}`);
  if (delta.claims > 0)
    bits.push(`+${delta.claims} claim${delta.claims === 1 ? "" : "s"}`);
  if (bits.length === 0) return null;

  return (
    <div className="flex items-center gap-2 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2 text-sm">
      <SparklesIcon className="size-4 shrink-0 text-primary" aria-hidden />
      <span>
        Since you last looked:{" "}
        <span className="font-medium">{bits.join(" · ")}</span>
      </span>
    </div>
  );
}
