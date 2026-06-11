"use client";

import { useEffect, useMemo, useRef } from "react";
import Link from "next/link";
import { ExternalLinkIcon } from "lucide-react";

import { QueryError } from "@/components/shared/QueryError";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { relativeTime, urlHost } from "@/lib/format";
import { useDocument } from "@/lib/queries";

/** What the sheet shows: a stored document, optionally with a quote to find. */
export interface DocumentSheetTarget {
  documentId: number;
  /** Verbatim evidence span to locate, highlight, and scroll to. */
  quote?: string;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface LocatedQuote {
  before: string;
  match: string;
  after: string;
}

/**
 * Finds `quote` in `content`, tolerant of whitespace differences (the
 * backend's span verifier compares whitespace-normalized, so the stored
 * quote may differ from the snapshot only in spacing/newlines).
 */
function locateQuote(content: string, quote: string): LocatedQuote | null {
  const words = quote.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return null;
  const pattern = words.map(escapeRegExp).join("\\s+");
  let re: RegExp;
  try {
    re = new RegExp(pattern, "i");
  } catch {
    return null;
  }
  const match = re.exec(content);
  if (!match) return null;
  return {
    before: content.slice(0, match.index),
    match: match[0],
    after: content.slice(match.index + match[0].length),
  };
}

function SheetSkeleton() {
  return (
    <div className="space-y-3 px-4">
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-2/3" />
      <Skeleton className="h-4 w-3/4" />
    </div>
  );
}

/**
 * The citation mechanic: a right-side sheet (~45% width) showing the stored
 * snapshot behind any quote. The quote is located by whitespace-tolerant
 * string search, highlighted, and scrolled into view. Reused from StanceCards
 * and (later) dossier views.
 */
export function DocumentSheet({
  target,
  onOpenChange,
}: {
  /** Null = closed. */
  target: DocumentSheetTarget | null;
  onOpenChange: (open: boolean) => void;
}) {
  const open = target !== null;
  const documentId = target?.documentId ?? NaN;
  const quote = target?.quote ?? "";
  const document = useDocument(documentId);

  const content = document.data?.content_text;
  const located = useMemo(() => {
    if (!content || !quote) return null;
    return locateQuote(content, quote);
  }, [content, quote]);

  const markRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (open && located) {
      // Defer one frame so the sheet's enter transition doesn't fight the scroll.
      const handle = requestAnimationFrame(() => {
        markRef.current?.scrollIntoView({ block: "center" });
      });
      return () => cancelAnimationFrame(handle);
    }
  }, [open, located]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="data-[side=right]:w-full data-[side=right]:sm:max-w-[45vw] data-[side=right]:sm:min-w-[28rem] gap-0 overflow-hidden"
      >
        {document.isPending ? (
          <>
            <SheetHeader>
              <SheetTitle>Loading document…</SheetTitle>
            </SheetHeader>
            <SheetSkeleton />
          </>
        ) : document.isError ? (
          <>
            <SheetHeader>
              <SheetTitle>Document unavailable</SheetTitle>
            </SheetHeader>
            <div className="px-4">
              <QueryError
                error={document.error}
                onRetry={() => void document.refetch()}
              />
            </div>
          </>
        ) : (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle className="line-clamp-2">
                {document.data.title ?? `Document #${document.data.id}`}
              </SheetTitle>
              <SheetDescription className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span>{document.data.source_name ?? "Manual ingest"}</span>
                <span aria-hidden>·</span>
                <span>fetched {relativeTime(document.data.fetched_at)}</span>
                {document.data.url ? (
                  <>
                    <span aria-hidden>·</span>
                    <a
                      href={document.data.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                    >
                      {urlHost(document.data.url)}
                      <ExternalLinkIcon className="size-3 shrink-0" aria-hidden />
                    </a>
                  </>
                ) : null}
              </SheetDescription>
              <div className="mt-1">
                <Button
                  variant="outline"
                  size="xs"
                  render={<Link href={`/documents/${document.data.id}`} />}
                >
                  Open full page
                </Button>
              </div>
            </SheetHeader>
            <div className="flex-1 overflow-y-auto p-4">
              {quote && !located ? (
                <p className="mb-4 rounded-lg border border-amber-300/60 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
                  The quoted span wasn&apos;t found verbatim in this snapshot —
                  showing the full text. Quote: &ldquo;{quote}&rdquo;
                </p>
              ) : null}
              {document.data.content_text ? (
                <div className="max-w-prose text-sm leading-7 whitespace-pre-wrap">
                  {located ? (
                    <>
                      {located.before}
                      <mark
                        ref={markRef}
                        className="rounded-sm bg-amber-200 px-0.5 text-foreground dark:bg-amber-500/40 dark:text-foreground"
                      >
                        {located.match}
                      </mark>
                      {located.after}
                    </>
                  ) : (
                    document.data.content_text
                  )}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No extracted text for this document.
                </p>
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
