"use client";

import { useRouter } from "next/navigation";

import { StatusChip, WatchHitChip } from "@/components/shared/StatusChip";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { DocumentListItem } from "@/lib/api";
import { absoluteTime, relativeTime } from "@/lib/format";

/**
 * The library's document table, shared with the entity page and search
 * results. Rows navigate to /documents/{id}; FTS snippets render whenever the
 * backend included them (only `q`-filtered listings do).
 */
export function DocumentTable({ items }: { items: DocumentListItem[] }) {
  const router = useRouter();

  return (
    <div className="rounded-xl border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Title</TableHead>
            <TableHead className="w-40">Source</TableHead>
            <TableHead className="w-32">Fetched</TableHead>
            <TableHead className="w-28">Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item) => (
            <TableRow
              key={item.id}
              className="cursor-pointer"
              onClick={() => router.push(`/documents/${item.id}`)}
            >
              <TableCell className="max-w-0">
                <p className="truncate font-medium">
                  {item.title ?? "(untitled document)"}
                </p>
                {item.snippet ? (
                  <p
                    className="mt-0.5 truncate text-xs text-muted-foreground [&_b]:font-semibold [&_b]:text-foreground [&_mark]:bg-amber-100 dark:[&_mark]:bg-amber-900"
                    // FTS5 snippet() output with highlight markers from our own backend.
                    dangerouslySetInnerHTML={{ __html: item.snippet }}
                  />
                ) : null}
              </TableCell>
              <TableCell className="text-muted-foreground">
                {item.source_name ?? "—"}
              </TableCell>
              <TableCell
                className="text-muted-foreground"
                title={absoluteTime(item.fetched_at)}
              >
                {relativeTime(item.fetched_at)}
              </TableCell>
              <TableCell>
                <span className="flex items-center gap-1.5">
                  <StatusChip status={item.enrichment_status} />
                  {item.watch_hit ? <WatchHitChip /> : null}
                </span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
