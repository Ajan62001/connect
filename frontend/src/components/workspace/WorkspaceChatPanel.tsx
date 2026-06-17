"use client";

import { useEffect, useRef, useState } from "react";
import {
  Loader2Icon,
  MessageSquarePlusIcon,
  SendIcon,
  SparklesIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { ApiError, getWorkspaceChat } from "@/lib/api";
import type { ChatTurn } from "@/lib/api";
import {
  useDeleteWorkspaceChat,
  useWorkspaceChat,
  useWorkspaceChats,
} from "@/lib/queries";

export function WorkspaceChatPanel({
  workspaceId,
  hero = false,
}: {
  workspaceId: number;
  hero?: boolean;
}) {
  const [chatId, setChatId] = useState<number | undefined>(undefined);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const chat = useWorkspaceChat(workspaceId);
  const chats = useWorkspaceChats(workspaceId);
  const del = useDeleteWorkspaceChat(workspaceId);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, chat.isPending]);

  const newChat = () => {
    setChatId(undefined);
    setTurns([]);
    setInput("");
  };

  const openChat = async (id: number) => {
    try {
      const detail = await getWorkspaceChat(workspaceId, id);
      setChatId(id);
      setTurns(detail.transcript);
    } catch (e) {
      toast.error("Could not open chat", {
        description: e instanceof Error ? e.message : undefined,
      });
    }
  };

  const send = () => {
    const text = input.trim();
    if (!text || chat.isPending) return;
    setInput("");
    // optimistic user turn
    setTurns((t) => [
      ...t,
      { role: "user", text, tools_used: [], finding_id: null, finding_title: null },
    ]);
    chat.mutate(
      { message: text, chatId },
      {
        onSuccess: (resp) => {
          setChatId(resp.chat_id);
          setTurns(resp.transcript);
        },
      },
    );
  };

  const errorText = chat.isError
    ? chat.error instanceof ApiError && chat.error.status === 429
      ? "Daily AI budget reached — try again later."
      : chat.error instanceof ApiError && chat.error.status === 503
        ? "AI is not configured on this deployment."
        : chat.error.message
    : null;

  const saved = chats.data ?? [];

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2">
          <SparklesIcon className="size-4" />
          Workspace assistant
        </CardTitle>
        <Button variant="ghost" size="xs" onClick={newChat}>
          <MessageSquarePlusIcon data-icon="inline-start" />
          New
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        {saved.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {saved.map((c) => (
              <span key={c.id} className="group inline-flex items-center">
                <Button
                  variant={c.id === chatId ? "secondary" : "outline"}
                  size="xs"
                  className="max-w-44 truncate"
                  onClick={() => void openChat(c.id)}
                >
                  {c.title || "Untitled"}
                </Button>
                <Button
                  variant="ghost"
                  size="xs"
                  aria-label="Delete chat"
                  className="px-1 opacity-0 group-hover:opacity-100"
                  onClick={() =>
                    del.mutate(c.id, {
                      onSuccess: () => {
                        if (c.id === chatId) newChat();
                      },
                    })
                  }
                >
                  <Trash2Icon className="size-3" />
                </Button>
              </span>
            ))}
          </div>
        ) : null}

        <div
          className={cn(
            "space-y-3 overflow-y-auto",
            hero
              ? "h-[clamp(360px,calc(100vh-360px),720px)]"
              : "max-h-96",
          )}
        >
          {turns.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Ask about this workspace’s news, or tell the assistant to capture
              a finding. It searches and reads only this workspace’s documents.
            </p>
          ) : (
            turns.map((t, i) => (
              <div key={i} className={t.role === "user" ? "text-right" : ""}>
                <div
                  className={`inline-block max-w-[90%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
                    t.role === "user"
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted"
                  }`}
                >
                  {t.text}
                </div>
                {t.role === "assistant" && t.tools_used.length > 0 ? (
                  <div className="mt-1 text-xs text-muted-foreground">
                    used: {Array.from(new Set(t.tools_used)).join(", ")}
                  </div>
                ) : null}
                {t.finding_id ? (
                  <div className="mt-1">
                    <Badge variant="secondary">
                      Finding posted: {t.finding_title}
                    </Badge>
                  </div>
                ) : null}
              </div>
            ))
          )}
          {chat.isPending ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" />
              Thinking…
            </div>
          ) : null}
          <div ref={endRef} />
        </div>

        {errorText ? (
          <p className="text-sm text-destructive">{errorText}</p>
        ) : null}

        <div className="flex gap-2">
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask the workspace assistant…"
            className="min-h-12"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <Button onClick={send} disabled={!input.trim() || chat.isPending}>
            <SendIcon className="size-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
