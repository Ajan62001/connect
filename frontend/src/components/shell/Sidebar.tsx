"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BookOpenIcon,
  DatabaseIcon,
  EyeIcon,
  FilmIcon,
  FlaskConicalIcon,
  LayoutGridIcon,
  LibraryBigIcon,
  NotebookPenIcon,
  RssIcon,
  ScaleIcon,
  SearchIcon,
  ShieldIcon,
  SunIcon,
  TelescopeIcon,
  UsersIcon,
} from "lucide-react";

import { useBriefToday, useMe, useOpenContradictionCount } from "@/lib/queries";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/today", label: "Today", icon: SunIcon },
  { href: "/workspaces", label: "Workspaces", icon: LayoutGridIcon },
  { href: "/feed", label: "Feed", icon: RssIcon },
  { href: "/library", label: "Library", icon: LibraryBigIcon },
  { href: "/search", label: "Search", icon: SearchIcon },
  { href: "/analyze", label: "Analyze", icon: FlaskConicalIcon },
  { href: "/investigations", label: "Investigations", icon: TelescopeIcon },
  { href: "/stories", label: "Stories", icon: BookOpenIcon },
  { href: "/content", label: "Content", icon: FilmIcon },
  { href: "/findings", label: "Findings", icon: NotebookPenIcon },
  { href: "/community", label: "Community", icon: UsersIcon },
  { href: "/contradictions", label: "Contradictions", icon: ScaleIcon },
  { href: "/sources", label: "Sources", icon: DatabaseIcon },
  { href: "/watchlist", label: "Watchlist", icon: EyeIcon },
] as const;

export function Sidebar() {
  const pathname = usePathname();

  // Gates the admin section only — every /api/admin route 403s server-side.
  const me = useMe();

  // Shared with the Today page (same query key); errors just mean no dot.
  const brief = useBriefToday();
  const hasUnseenBriefItems =
    brief.data !== undefined &&
    Object.values(brief.data.sections).some((items) =>
      (items ?? []).some((item) => !item.seen),
    );

  // Cheap count (page_size=1, total only); errors just mean no badge.
  const openContradictions = useOpenContradictionCount();
  const contradictionCount = openContradictions.data ?? 0;

  return (
    <aside className="flex w-52 shrink-0 flex-col border-r bg-sidebar">
      <div className="flex h-14 items-center border-b px-4">
        <Link href="/feed" className="text-base font-semibold tracking-tight">
          connect
        </Link>
      </div>
      <nav className="flex flex-1 flex-col gap-1 p-2">
        {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
          const active =
            pathname === href ||
            pathname.startsWith(`${href}/`) ||
            (href === "/library" && pathname.startsWith("/documents")) ||
            (href === "/search" && pathname.startsWith("/entity")) ||
            (href === "/analyze" && pathname.startsWith("/analysis")) ||
            (href === "/investigations" &&
              pathname.startsWith("/investigation")) ||
            (href === "/stories" && pathname.startsWith("/story/")) ||
            (href === "/today" &&
              (pathname.startsWith("/brief") ||
                pathname.startsWith("/thread") ||
                pathname.startsWith("/event/")));
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                active
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
              )}
            >
              <Icon className="size-4" aria-hidden />
              {label}
              {href === "/today" && hasUnseenBriefItems ? (
                <span
                  className="ml-auto size-2 rounded-full bg-primary"
                  title="Unseen brief items"
                  aria-label="Unseen brief items"
                />
              ) : null}
              {href === "/contradictions" && contradictionCount > 0 ? (
                <span
                  className="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-primary px-1.5 text-[11px] font-semibold tabular-nums text-primary-foreground"
                  title={`${contradictionCount} open contradiction${contradictionCount === 1 ? "" : "s"}`}
                >
                  {contradictionCount > 99 ? "99+" : contradictionCount}
                </span>
              ) : null}
            </Link>
          );
        })}
      </nav>
      {me.data?.role === "admin" ? (
        <div className="border-t p-2">
          <Link
            href="/admin/users"
            className={cn(
              "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              pathname.startsWith("/admin")
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
            )}
          >
            <ShieldIcon className="size-4" aria-hidden />
            Admin
          </Link>
        </div>
      ) : null}
      <div className="border-t p-3 text-xs text-muted-foreground">
        Phase 3 · analysis &amp; verification
      </div>
    </aside>
  );
}
