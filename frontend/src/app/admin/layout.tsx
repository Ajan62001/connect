"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ShieldXIcon } from "lucide-react";

import { EmptyState } from "@/components/shared/EmptyState";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useMe } from "@/lib/queries";

const ADMIN_TABS = [
  { href: "/admin/users", label: "Users" },
  { href: "/admin/invites", label: "Invites" },
  { href: "/admin/settings", label: "Settings" },
  { href: "/admin/spend", label: "Spend" },
] as const;

/**
 * Admin-only route group (design §6). The gate is cosmetic-defensive only:
 * every /api/admin endpoint is behind require_admin server-side, so a
 * member who hand-navigates here gets a friendly panel instead of a wall
 * of 403s. AppShell has already resolved /api/me before this renders.
 */
export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const me = useMe();

  if (!me.data) {
    // AppShell's AuthGate means this is a brief flash at most.
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (me.data.role !== "admin") {
    return (
      <EmptyState
        icon={ShieldXIcon}
        title="Admins only"
        description="This area manages users, invites and deployment budgets. Ask an admin if you need something changed."
        action={
          <Button variant="outline" size="sm" render={<Link href="/feed" />}>
            Back to the feed
          </Button>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Admin</h1>
        <p className="text-sm text-muted-foreground">
          Members, invites and deployment-wide budgets.
        </p>
      </div>
      <nav className="flex items-center gap-1 border-b">
        {ADMIN_TABS.map(({ href, label }) => {
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
                active
                  ? "border-foreground text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {label}
            </Link>
          );
        })}
      </nav>
      {children}
    </div>
  );
}
