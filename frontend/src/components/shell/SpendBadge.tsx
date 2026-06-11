"use client";

import { CircleDollarSignIcon } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useSpend } from "@/lib/queries";

/**
 * Compact "today $X.XX / $cap" pill with a 7-day breakdown tooltip. Renders
 * nothing while loading or when /api/spend is unavailable (Phase 1 backend
 * not running yet) — spend visibility is informational, never blocking.
 */
export function SpendBadge() {
  const spend = useSpend(7);
  if (!spend.data) return null;

  const { daily_cap_usd, today_spent_usd, days } = spend.data;
  const hot = daily_cap_usd > 0 && today_spent_usd / daily_cap_usd > 0.8;

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <span
            className={cn(
              "flex cursor-default items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
              hot
                ? "bg-destructive/10 text-destructive dark:bg-destructive/20"
                : "text-muted-foreground hover:bg-muted",
            )}
          />
        }
      >
        <CircleDollarSignIcon className="size-3.5" aria-hidden />
        <span>
          {formatUsd(today_spent_usd)}{" "}
          <span className={hot ? "" : "text-muted-foreground/70"}>
            / {formatUsd(daily_cap_usd)}
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-1.5">
          <p className="font-medium">
            LLM spend — today {formatUsd(today_spent_usd)} of{" "}
            {formatUsd(daily_cap_usd)} cap
          </p>
          {days.length === 0 ? (
            <p className="text-muted-foreground">No calls in the last 7 days.</p>
          ) : (
            <table className="w-full">
              <tbody>
                {days.map((day) => (
                  <tr key={day.day} className="text-muted-foreground">
                    <td className="pr-3 font-mono">{day.day}</td>
                    <td className="pr-3 text-right tabular-nums">
                      {day.calls} call{day.calls === 1 ? "" : "s"}
                    </td>
                    <td className="text-right font-medium tabular-nums text-foreground">
                      {formatUsd(day.cost_usd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
