"use client";

import { Loader2Icon, XIcon, ZapIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import type { PositionShift, Statement } from "@/lib/api";
import { formatDay } from "@/lib/format";
import { useDismissShift } from "@/lib/queries";

const SHIFT_KIND_LABELS: Record<string, string> = {
  shifted: "Shifted position",
  reversed: "Reversed position",
};

export function shiftKindLabel(kind: string): string {
  return SHIFT_KIND_LABELS[kind] ?? kind;
}

/**
 * Amber banner between two PositionCards on the timeline: "⚡ reversed
 * position — <note> · was <from_date>, now <to_date>", with a dismiss x.
 * `from`/`to` are looked up from the statement list and may be undefined
 * (the banner then renders without the date line).
 */
export function ShiftBanner({
  shift,
  entityId,
  from,
  to,
}: {
  shift: PositionShift;
  entityId: number;
  from: Statement | undefined;
  to: Statement | undefined;
}) {
  const dismiss = useDismissShift(entityId);

  const fromDay = from?.stated_at ? formatDay(from.stated_at) : null;
  const toDay = to?.stated_at ? formatDay(to.stated_at) : null;
  const dateBits: string[] = [];
  if (fromDay) dateBits.push(`was ${fromDay}`);
  if (toDay) dateBits.push(`now ${toDay}`);

  return (
    <div className="flex items-start gap-2 rounded-lg border border-amber-300/70 bg-amber-50 px-3 py-2 dark:border-amber-800 dark:bg-amber-950/40">
      <ZapIcon
        className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400"
        aria-hidden
      />
      <div className="min-w-0 flex-1 space-y-0.5 text-sm">
        <p className="font-medium text-amber-900 dark:text-amber-200">
          {shiftKindLabel(shift.kind)}
          {shift.note ? (
            <span className="font-normal"> — {shift.note}</span>
          ) : null}
        </p>
        {dateBits.length > 0 ? (
          <p className="text-xs text-amber-800/80 dark:text-amber-300/80">
            {dateBits.join(", ")}
          </p>
        ) : null}
      </div>
      <Button
        variant="ghost"
        size="icon-xs"
        className="text-amber-700 hover:bg-amber-100 dark:text-amber-400 dark:hover:bg-amber-900/50"
        aria-label="Dismiss shift"
        title="Dismiss shift"
        disabled={dismiss.isPending}
        onClick={() =>
          dismiss.mutate(shift.id, {
            onSuccess: () => toast.success("Shift dismissed"),
            onError: (error) =>
              toast.error("Could not dismiss shift", {
                description: error.message,
              }),
          })
        }
      >
        {dismiss.isPending ? (
          <Loader2Icon className="animate-spin" aria-hidden />
        ) : (
          <XIcon aria-hidden />
        )}
      </Button>
    </div>
  );
}
