"use client";

import { Fragment, type ReactNode } from "react";
import Link from "next/link";

import type { StorySource } from "@/lib/api";
import { cn } from "@/lib/utils";

/** [[E3]] citation markers the synthesis call emits over the closed menu. */
const MARKER_RE = /\[\[(E\d+)\]\]/g;
const BOLD_RE = /\*\*([^*]+)\*\*/g;

export type SourcesByRef = Record<string, StorySource>;

export function buildSourcesByRef(sources: StorySource[]): SourcesByRef {
  const map: SourcesByRef = {};
  for (const s of sources) map[s.ref] = s;
  return map;
}

function CitationChip({
  refId,
  sources,
}: {
  refId: string;
  sources: SourcesByRef;
}) {
  const source = sources[refId];
  const n = refId.replace(/^E/, "");
  const title = source
    ? `${source.source_name ?? source.title ?? "source"}${
        source.quote ? `: “${source.quote}”` : ""
      }`
    : "unknown source";
  const chip = (
    <sup
      className={cn(
        "ml-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded bg-muted px-1 text-[10px] font-medium tabular-nums text-muted-foreground align-super",
        source?.document_id != null && "hover:bg-primary hover:text-primary-foreground",
      )}
    >
      {n}
    </sup>
  );
  if (source?.document_id != null) {
    return (
      <Link href={`/documents/${source.document_id}`} title={title}>
        {chip}
      </Link>
    );
  }
  return <span title={title}>{chip}</span>;
}

function renderEmphasis(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(BOLD_RE)) {
    const index = match.index ?? 0;
    if (index > last) nodes.push(text.slice(last, index));
    nodes.push(<strong key={`${keyPrefix}-b${i++}`}>{match[1]}</strong>);
    last = index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function renderInline(
  text: string,
  keyPrefix: string,
  sources: SourcesByRef,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(MARKER_RE)) {
    const index = match.index ?? 0;
    if (index > last) {
      nodes.push(
        <Fragment key={`${keyPrefix}-t${i}`}>
          {renderEmphasis(text.slice(last, index), `${keyPrefix}-t${i}`)}
        </Fragment>,
      );
    }
    nodes.push(
      <CitationChip key={`${keyPrefix}-c${i}`} refId={match[1]} sources={sources} />,
    );
    last = index + match[0].length;
    i += 1;
  }
  if (last < text.length) {
    nodes.push(
      <Fragment key={`${keyPrefix}-tail`}>
        {renderEmphasis(text.slice(last), `${keyPrefix}-tail`)}
      </Fragment>,
    );
  }
  return nodes;
}

/**
 * Small markdown renderer for a story's narrative_md: #/## headings, -/* lists,
 * **bold**, and the [[E#]] citation markers -> chips that link to the cited
 * document. No markdown dependency (matches the investigation NarrativeMarkdown).
 */
export function StoryNarrative({
  markdown,
  sources,
  className,
}: {
  markdown: string;
  sources: SourcesByRef;
  className?: string;
}) {
  const blocks = markdown
    .split(/\n[ \t]*\n+/)
    .map((b) => b.trim())
    .filter(Boolean);

  return (
    <div className={cn("max-w-prose space-y-3 text-sm leading-7", className)}>
      {blocks.map((block, b) => {
        const heading = /^(#{1,6})[ \t]+([^\n]*)$/.exec(block);
        if (heading) {
          return (
            <h3
              key={b}
              className="pt-3 text-base font-semibold tracking-tight first:pt-0"
            >
              {renderInline(heading[2], `h${b}`, sources)}
            </h3>
          );
        }
        const lines = block.split("\n");
        if (lines.every((line) => /^\s*[-*]\s+/.test(line))) {
          return (
            <ul key={b} className="list-disc space-y-1 pl-5">
              {lines.map((line, l) => (
                <li key={l}>
                  {renderInline(line.replace(/^\s*[-*]\s+/, ""), `b${b}l${l}`, sources)}
                </li>
              ))}
            </ul>
          );
        }
        return <p key={b}>{renderInline(block, `b${b}`, sources)}</p>;
      })}
    </div>
  );
}
