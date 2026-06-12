"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";

import { Sidebar } from "@/components/shell/Sidebar";
import { Topbar } from "@/components/shell/Topbar";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { useMe } from "@/lib/queries";

/**
 * Blocks the shell until /api/me resolves. A 401 means signed out: the
 * fetch wrapper already issued the hard redirect; the router.replace here
 * is the belt-and-braces (and keeps tests/SSR honest). Anything else
 * (backend down) renders a plain retry screen instead of a shell whose
 * every query is failing.
 */
function AuthGate({ children }: { children: React.ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const unauthorized =
    me.error instanceof ApiError && me.error.status === 401;

  useEffect(() => {
    if (unauthorized) router.replace("/signin");
  }, [unauthorized, router]);

  if (me.data) return <>{children}</>;

  if (me.isError && !unauthorized) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <p className="text-sm font-medium">
          Can&apos;t reach the connect API
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          {me.error instanceof ApiError
            ? me.error.detail
            : "Unexpected error"}{" "}
          — check the API server, then reload.
        </p>
      </div>
    );
  }

  // loading (or the 401 redirect is in flight)
  return (
    <div className="flex h-full">
      <div className="w-52 shrink-0 border-r bg-sidebar" />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="h-14 shrink-0 border-b" />
        <div className="space-y-4 p-6">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-32 w-full" />
        </div>
      </div>
    </div>
  );
}

/**
 * Session-aware shell: /signin renders bare (it IS the signed-out
 * surface); everything else renders sidebar + topbar behind the auth gate.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (pathname === "/signin") return <>{children}</>;

  return (
    <AuthGate>
      <div className="flex h-full">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <Topbar />
          <main className="flex-1 overflow-y-auto">
            <div className="mx-auto w-full max-w-6xl space-y-6 p-6">
              {children}
            </div>
          </main>
        </div>
      </div>
    </AuthGate>
  );
}
