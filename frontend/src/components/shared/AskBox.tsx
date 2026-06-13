"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, SparklesIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { DocumentAnswer } from "@/lib/api";

/** Grounded "cross-question" box — ask a free-text question and show the
 *  answer (with its supporting quote). Reused by stories, posts, documents. */
export function AskBox({
  onAsk,
  placeholder = "Ask a follow-up…",
}: {
  onAsk: (question: string) => Promise<DocumentAnswer>;
  placeholder?: string;
}) {
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<DocumentAnswer | null>(null);
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    const q = question.trim();
    if (!q || loading) return;
    setLoading(true);
    try {
      setAnswer(await onAsk(q));
      void queryClient.invalidateQueries({ queryKey: ["spend"] });
    } catch (e) {
      toast.error("Could not answer", {
        description: e instanceof Error ? e.message : undefined,
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <Input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={placeholder}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <Button
          size="sm"
          disabled={!question.trim() || loading}
          onClick={() => void submit()}
        >
          {loading ? (
            <Loader2Icon className="animate-spin" data-icon="inline-start" />
          ) : (
            <SparklesIcon data-icon="inline-start" />
          )}
          Ask
        </Button>
      </div>
      {answer ? (
        <div className="space-y-1.5 rounded-lg border bg-muted/40 p-3 text-sm">
          <p className="whitespace-pre-wrap">{answer.answer}</p>
          {answer.grounded && answer.quote ? (
            <p className="border-l-2 pl-2 text-xs italic text-muted-foreground">
              “{answer.quote}”
            </p>
          ) : !answer.grounded ? (
            <p className="text-xs text-muted-foreground">
              Not covered by the available facts.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
