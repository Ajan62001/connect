"use client";

import { useState } from "react";
import { Loader2Icon, PlayIcon, XIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { useWorkspaceDeepRun } from "@/lib/queries";

function StatusBadge({ status }: { status: string }) {
  const variant =
    status === "done"
      ? "default"
      : status === "error"
        ? "outline"
        : "secondary";
  return <Badge variant={variant}>{status}</Badge>;
}

export function WorkspaceTasks({ workspaceId }: { workspaceId: number }) {
  const [prompt, setPrompt] = useState("");
  const { state, start, cancel, reset } = useWorkspaceDeepRun(workspaceId);
  const running = state.status === "running";

  const run = () => {
    const p = prompt.trim();
    if (!p || running) return;
    void start(p);
    setPrompt("");
  };

  return (
    <div className="space-y-3">
      <Textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder="Give the agent a longer task — e.g. “review this week’s coverage and summarise the key shifts”…"
        className="min-h-16"
        disabled={running}
      />
      <div className="flex gap-2">
        <Button onClick={run} disabled={!prompt.trim() || running}>
          {running ? (
            <Loader2Icon className="animate-spin" data-icon="inline-start" />
          ) : (
            <PlayIcon data-icon="inline-start" />
          )}
          Run a task
        </Button>
        {running ? (
          <Button variant="outline" onClick={cancel}>
            <XIcon data-icon="inline-start" />
            Cancel
          </Button>
        ) : state.status !== "idle" ? (
          <Button variant="ghost" onClick={reset}>
            New task
          </Button>
        ) : null}
      </div>

      {state.status !== "idle" ? (
        <Card>
          <CardContent className="space-y-2 py-3">
            <div className="flex items-center gap-2">
              <StatusBadge status={state.status} />
              {running ? (
                <Loader2Icon className="size-4 animate-spin text-muted-foreground" />
              ) : null}
            </div>
            <p className="text-sm font-medium">{state.prompt}</p>
            {state.lines.length > 0 ? (
              <ul className="space-y-0.5 text-xs text-muted-foreground">
                {state.lines.map((line, i) => (
                  <li key={i}>· {line}</li>
                ))}
              </ul>
            ) : running ? (
              <p className="text-xs text-muted-foreground">starting…</p>
            ) : null}
            {state.status === "done" && state.answer ? (
              <p className="text-sm whitespace-pre-wrap">{state.answer.text}</p>
            ) : null}
            {state.status === "done" && !state.answer ? (
              <p className="text-sm text-muted-foreground">
                The run finished without an answer.
              </p>
            ) : null}
            {state.status === "error" && state.error ? (
              <p className="text-sm text-destructive">{state.error}</p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
