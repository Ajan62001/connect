"use client";

import { useState } from "react";
import { CircleDollarSignIcon } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { SpendSlice } from "@/lib/api";
import { formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useMe, useSpend } from "@/lib/queries";

/**
 * Compact "today $X.XX / $cap" pill with a 7-day breakdown tooltip.
 *
 * v0.2 Phase D: the pill shows MY spend against MY cap (members see only
 * their own ledger); admins can click it to toggle to the system-wide view
 * (global envelope across all users + system jobs). Against a pre-Phase-D
 * backend /api/spend carries no per-user slice and the pill falls back to
 * the single global ledger for everyone — spend visibility is
 * informational, never blocking, so an unavailable endpoint renders
 * nothing.
 */
export function SpendBadge() {
  const me = useMe();
  const spend = useSpend(7);
  // Admin-only scope toggle; members are always on their own ledger.
  const [systemView, setSystemView] = useState(false);

  if (!spend.data) return null;
  const { mine, global } = spend.data;
  if (!mine && !global) return null;

  const isAdmin = me.data?.role === "admin";
  // The toggle needs both slices to mean anything; a legacy backend serves
  // only the global ledger and everyone just sees that.
  const canToggle = isAdmin && mine !== null && global !== null;
  const showSystem = (systemView && canToggle) || mine === null;
  const slice: SpendSlice = (showSystem ? global : mine) ?? (global as SpendSlice);

  const hot =
    slice.cap_usd !== null &&
    slice.cap_usd > 0 &&
    slice.today_usd / slice.cap_usd > 0.8;
  const scopeLabel = mine === null ? null : showSystem ? "system" : "me";

  const pillBody = (
    <>
      <CircleDollarSignIcon className="size-3.5" aria-hidden />
      {scopeLabel ? (
        <span className="uppercase tracking-wide text-[10px] opacity-70">
          {scopeLabel}
        </span>
      ) : null}
      <span>
        {formatUsd(slice.today_usd)}
        {slice.cap_usd !== null ? (
          <span className={hot ? "" : " text-muted-foreground/70"}>
            {" "}
            / {formatUsd(slice.cap_usd)}
          </span>
        ) : null}
      </span>
    </>
  );

  const pillClass = cn(
    "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
    canToggle ? "cursor-pointer" : "cursor-default",
    hot
      ? "bg-destructive/10 text-destructive dark:bg-destructive/20"
      : "text-muted-foreground hover:bg-muted",
  );

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          canToggle ? (
            <button
              type="button"
              className={pillClass}
              aria-label={
                showSystem
                  ? "System spend — click for your spend"
                  : "Your spend — click for system spend"
              }
              onClick={() => setSystemView((value) => !value)}
            />
          ) : (
            <span className={pillClass} />
          )
        }
      >
        {pillBody}
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-1.5">
          <p className="font-medium">
            {showSystem || mine === null
              ? "System LLM spend (all users + system jobs)"
              : "Your LLM spend"}{" "}
            — today {formatUsd(slice.today_usd)}
            {slice.cap_usd !== null
              ? ` of ${formatUsd(slice.cap_usd)} cap`
              : ""}
          </p>
          {slice.investigation_cap_usd !== null ? (
            <p className="text-muted-foreground">
              Investigations have their own{" "}
              {formatUsd(slice.investigation_cap_usd)}/day envelope.
            </p>
          ) : null}
          {slice.days.length === 0 ? (
            <p className="text-muted-foreground">No calls in the last 7 days.</p>
          ) : (
            <table className="w-full">
              <tbody>
                {slice.days.map((day) => (
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
          {canToggle ? (
            <p className="text-muted-foreground">
              Click to switch to {showSystem ? "your" : "the system"} view.
            </p>
          ) : null}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
