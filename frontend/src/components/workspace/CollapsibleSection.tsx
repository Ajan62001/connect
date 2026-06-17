"use client";

import { useState, type ReactNode } from "react";
import { ChevronDownIcon, type LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * A disclosure row for the workspace Context rail: an icon + title + optional
 * count badge that expands to reveal its section. Keeps every feature on the
 * page while collapsing the ones you're not using, so the assistant stays the
 * focus.
 */
export function CollapsibleSection({
  icon: Icon,
  title,
  count,
  hint,
  defaultOpen = false,
  children,
}: {
  icon: LucideIcon;
  title: string;
  count?: number;
  hint?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-accent/40"
      >
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <span className="flex-1 truncate text-sm font-medium">{title}</span>
        {typeof count === "number" && count > 0 ? (
          <Badge variant="secondary" className="tabular-nums">
            {count}
          </Badge>
        ) : null}
        <ChevronDownIcon
          className={cn(
            "size-4 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {open ? (
        <div className="space-y-3 border-t p-3">
          {hint ? (
            <p className="text-xs text-muted-foreground">{hint}</p>
          ) : null}
          {children}
        </div>
      ) : null}
    </div>
  );
}
