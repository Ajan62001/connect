"use client";

import { Fragment, type ReactNode } from "react";

import type { DocumentSheetTarget } from "@/components/documents/DocumentSheet";
import {
  CitationChip,
  type FindingsById,
} from "@/components/investigation/CitationChip";
import { cn } from "@/lib/utils";

/** [[f12]] citation markers emitted by the synthesis call. */
const MARKER_RE = /\[\[f(\d+)\]\]/g;
/** Minimal inline emphasis — the synthesis prompt only uses ** for bold. */
const BOLD_RE = /\*\*([^*]+)\*\*/g;

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
  findings: FindingsById,
  onOpenDocument: (target: DocumentSheetTarget) => void,
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
      <CitationChip
        key={`${keyPrefix}-f${i}`}
        findingId={Number(match[1])}
        findings={findings}
        onOpenDocument={onOpenDocument}
      />,
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
 * Deliberately small markdown renderer for synthesis output (narrative_md,
 * actor motive_md): paragraphs, #/##/### headings, simple -/* lists, **bold**,
 * and the [[f#]] citation markers -> CitationChip -> DocumentSheet. No
 * markdown dependency; anything fancier renders as plain text, which is the
 * right degradation for grounded prose.
 */
export function NarrativeMarkdown({
  markdown,
  findings,
  onOpenDocument,
  className,
}: {
  markdown: string;
  findings: FindingsById;
  onOpenDocument: (target: DocumentSheetTarget) => void;
  className?: string;
}) {
  const blocks = markdown
    .split(/\n[ \t]*\n+/)
    .map((block) => block.trim())
    .filter(Boolean);

  return (
    <div className={cn("max-w-prose space-y-3 text-sm leading-7", className)}>
      {blocks.map((block, b) => {
        // Single-line headings only; multi-line blocks fall through to <p>.
        const heading = /^(#{1,6})[ \t]+([^\n]*)$/.exec(block);
        if (heading) {
          return (
            <h3
              key={b}
              className="pt-2 text-sm font-semibold tracking-tight first:pt-0"
            >
              {renderInline(heading[2], `h${b}`, findings, onOpenDocument)}
            </h3>
          );
        }
        const lines = block.split("\n");
        const isList = lines.every((line) => /^\s*[-*]\s+/.test(line));
        if (isList) {
          return (
            <ul key={b} className="list-disc space-y-1 pl-5">
              {lines.map((line, l) => (
                <li key={l}>
                  {renderInline(
                    line.replace(/^\s*[-*]\s+/, ""),
                    `b${b}l${l}`,
                    findings,
                    onOpenDocument,
                  )}
                </li>
              ))}
            </ul>
          );
        }
        return (
          <p key={b}>{renderInline(block, `b${b}`, findings, onOpenDocument)}</p>
        );
      })}
    </div>
  );
}
