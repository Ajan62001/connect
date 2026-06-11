"use client";

import { MessageSquareQuoteIcon } from "lucide-react";

import { EntityPill } from "@/components/entities/EntityPill";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { DocumentStatement } from "@/lib/api";

/**
 * Statements panel on the document page: speaker pill, verbatim quote, topic
 * chips, stance paraphrase. Renders nothing when the document carries no
 * statements (including pre-v9 backends that omit the field).
 */
export function StatementsCard({
  statements,
}: {
  statements: DocumentStatement[];
}) {
  if (statements.length === 0) return null;

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessageSquareQuoteIcon
            className="size-4 text-muted-foreground"
            aria-hidden
          />
          Statements
        </CardTitle>
        <CardDescription>
          Attributed positions extracted from this document — quotes are
          verbatim.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y">
          {statements.map((statement) => (
            <li
              key={statement.id}
              className="space-y-1.5 py-3 first:pt-0 last:pb-0"
            >
              <div className="flex flex-wrap items-center gap-1.5">
                <EntityPill entity={statement.speaker} />
                {(statement.topics ?? []).map((topic) => (
                  <Badge key={topic} variant="secondary">
                    {topic}
                  </Badge>
                ))}
              </div>
              <blockquote className="border-l-2 border-border pl-3 text-sm leading-6">
                &ldquo;{statement.quote}&rdquo;
              </blockquote>
              {statement.position_summary ? (
                <p className="text-xs text-muted-foreground italic">
                  {statement.position_summary}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
