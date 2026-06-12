"use client";

import { LogOutIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { Me } from "@/lib/api";
import { useLogout, useMe } from "@/lib/queries";

function initials(user: Me): string {
  const source = user.name?.trim() || user.email;
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

function Avatar({ user }: { user: Me }) {
  if (user.avatar_url) {
    return (
      // Plain <img>: Google avatar hosts reject hotlinks without
      // no-referrer, and next/image gains nothing for a 28px circle.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={user.avatar_url}
        alt=""
        referrerPolicy="no-referrer"
        className="size-7 rounded-full border"
      />
    );
  }
  return (
    <span className="flex size-7 items-center justify-center rounded-full bg-primary/10 text-[11px] font-semibold text-primary">
      {initials(user)}
    </span>
  );
}

/**
 * Session-aware corner of the Topbar: avatar + name (+ admin tag), and
 * sign out. Renders nothing while /api/me is in flight — the AppShell's
 * auth gate means it resolves before any page content anyway.
 */
export function UserMenu() {
  const me = useMe();
  const logout = useLogout();

  if (!me.data) return null;
  const user = me.data;

  return (
    <div className="flex items-center gap-2">
      <Avatar user={user} />
      <div className="hidden min-w-0 leading-tight md:block">
        <p className="truncate text-xs font-medium">
          {user.name ?? user.email}
          {user.role === "admin" ? (
            <span className="ml-1.5 rounded bg-primary/10 px-1 py-px text-[10px] font-semibold uppercase tracking-wide text-primary">
              admin
            </span>
          ) : null}
        </p>
        <p className="truncate text-[11px] text-muted-foreground">
          {user.email}
        </p>
      </div>
      <Tooltip>
        <TooltipTrigger
          render={
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Sign out"
              disabled={logout.isPending}
              onClick={() => logout.mutate()}
            />
          }
        >
          <LogOutIcon />
        </TooltipTrigger>
        <TooltipContent>Sign out</TooltipContent>
      </Tooltip>
    </div>
  );
}
