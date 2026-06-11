"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  DatabaseIcon,
  EyeIcon,
  LibraryBigIcon,
  RssIcon,
  SearchIcon,
  SunIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/today", label: "Today", icon: SunIcon },
  { href: "/feed", label: "Feed", icon: RssIcon },
  { href: "/library", label: "Library", icon: LibraryBigIcon },
  { href: "/search", label: "Search", icon: SearchIcon },
  { href: "/sources", label: "Sources", icon: DatabaseIcon },
  { href: "/watchlist", label: "Watchlist", icon: EyeIcon },
] as const;

export function Sidebar() {
  const pathname = usePathname();

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
            (href === "/search" && pathname.startsWith("/entity"));
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
            </Link>
          );
        })}
      </nav>
      <div className="border-t p-3 text-xs text-muted-foreground">
        Phase 1 · entities &amp; enrichment
      </div>
    </aside>
  );
}
