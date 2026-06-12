"use client";

import { useId } from "react";
import { GlobeIcon, LockIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import type { Visibility } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMe } from "@/lib/queries";

/**
 * Tenancy UI primitives (v0.2 Phase C, design §6). Every field these read is
 * optional in the API contract, so all surfaces render unchanged against a
 * pre-tenancy backend: undefined visibility/owner simply hides the chips.
 */

/**
 * Visibility chip. By default only `private` renders a chip — shared is the
 * norm and chip-on-every-row is noise; pass `showShared` on detail headers
 * where the explicit state is worth a chip either way.
 */
export function VisibilityBadge({
  visibility,
  showShared = false,
  className,
}: {
  visibility?: Visibility;
  showShared?: boolean;
  className?: string;
}) {
  if (visibility === "private") {
    return (
      <Badge
        variant="secondary"
        className={cn(
          "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
          className,
        )}
      >
        <LockIcon aria-hidden />
        private
      </Badge>
    );
  }
  if (visibility === "shared" && showShared) {
    return (
      <Badge variant="outline" className={cn("text-muted-foreground", className)}>
        <GlobeIcon aria-hidden />
        shared
      </Badge>
    );
  }
  return null;
}

/**
 * Owner attribution on shared work: "by <name>" (flat owner_id/owner_name,
 * mirroring the backend rows). Resolves "you" against the cached /api/me;
 * renders nothing for system-owned rows (owner_id null/absent).
 */
export function OwnerByline({
  ownerId,
  ownerName,
  className,
}: {
  ownerId?: number | null;
  ownerName?: string | null;
  className?: string;
}) {
  const me = useMe();
  if (ownerId == null) return null;
  const label =
    me.data != null && ownerId === me.data.id
      ? "you"
      : (ownerName ?? `member #${ownerId}`);
  return (
    <span className={cn("text-xs text-muted-foreground", className)}>
      by {label}
    </span>
  );
}

/**
 * The creation-form visibility toggle (design §1: dossiers default 'shared' —
 * community compounding is the point; private is the opt-in). The hint line
 * flips with the value so the consequence is always spelled out.
 */
export function VisibilityToggle({
  value,
  onChange,
  disabled = false,
  kind,
}: {
  value: Visibility;
  onChange: (value: Visibility) => void;
  disabled?: boolean;
  /** Lowercase noun for the hint copy, e.g. "analysis" | "investigation". */
  kind: string;
}) {
  const id = useId();
  return (
    <div className="flex items-start justify-between gap-4 rounded-lg border bg-muted/30 px-3 py-2.5">
      <div className="min-w-0 space-y-1 text-left">
        <Label htmlFor={id}>Share with everyone</Label>
        <p className="text-xs leading-snug text-muted-foreground">
          {value === "shared"
            ? `Shared ${kind === "analysis" ? "analyses" : `${kind}s`} are readable by every member and their grounded findings compound the community knowledge base.`
            : `Private — only you can see this ${kind}, and its findings stay out of the shared knowledge graph until you share it.`}
        </p>
      </div>
      <Switch
        id={id}
        checked={value === "shared"}
        disabled={disabled}
        aria-label="Share with everyone"
        onCheckedChange={(checked) => onChange(checked ? "shared" : "private")}
        className="mt-0.5"
      />
    </div>
  );
}
