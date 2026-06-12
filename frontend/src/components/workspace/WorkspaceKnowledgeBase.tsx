"use client";

import { useState } from "react";
import Link from "next/link";
import { FilePlus2Icon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { relativeTime } from "@/lib/format";
import {
  useAddWorkspaceDocument,
  useWorkspaceDocuments,
} from "@/lib/queries";

type Mode = "url" | "text" | "file";

function AddDialog({
  workspaceId,
  open,
  onOpenChange,
}: {
  workspaceId: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [mode, setMode] = useState<Mode>("url");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const add = useAddWorkspaceDocument(workspaceId);

  const submit = () => {
    const opts = {
      onSuccess: () => {
        toast.success("Added to the knowledge base");
        onOpenChange(false);
        setUrl("");
        setText("");
        setTitle("");
        setFile(null);
      },
      onError: (e: Error) =>
        toast.error("Could not add", { description: e.message }),
    };
    if (mode === "url" && url.trim()) {
      add.mutate({ mode: "url", url: url.trim() }, opts);
    } else if (mode === "text" && text.trim()) {
      add.mutate(
        { mode: "text", payload: { text: text.trim(), title: title.trim() || undefined } },
        opts,
      );
    } else if (mode === "file" && file) {
      add.mutate({ mode: "file", file }, opts);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add to knowledge base</DialogTitle>
        </DialogHeader>
        <div className="flex gap-1.5">
          {(["url", "text", "file"] as Mode[]).map((m) => (
            <Button
              key={m}
              variant={mode === m ? "secondary" : "outline"}
              size="xs"
              onClick={() => setMode(m)}
            >
              {m.toUpperCase()}
            </Button>
          ))}
        </div>
        {mode === "url" ? (
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
          />
        ) : mode === "text" ? (
          <div className="space-y-2">
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Title (optional)"
            />
            <Textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Paste text…"
              className="min-h-28"
            />
          </div>
        ) : (
          <Input
            type="file"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        )}
        <DialogFooter>
          <Button onClick={submit} disabled={add.isPending}>
            {add.isPending ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : null}
            Add
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function WorkspaceKnowledgeBase({ workspaceId }: { workspaceId: number }) {
  const [open, setOpen] = useState(false);
  const docs = useWorkspaceDocuments(workspaceId, 1);

  return (
    <div className="space-y-2">
      <Button
        variant="outline"
        size="sm"
        className="w-full"
        onClick={() => setOpen(true)}
      >
        <FilePlus2Icon data-icon="inline-start" />
        Add document
      </Button>
      <AddDialog
        workspaceId={workspaceId}
        open={open}
        onOpenChange={setOpen}
      />
      {docs.isPending ? (
        <Skeleton className="h-16 w-full" />
      ) : docs.isError || docs.data.total === 0 ? (
        <p className="text-sm text-muted-foreground">
          No documents in this workspace’s knowledge base yet. Add a URL, paste
          text, or upload a file — they join the feed and the assistant’s
          searches.
        </p>
      ) : (
        docs.data.items.map((d) => (
          <Card key={d.id}>
            <CardContent className="py-2">
              <Link
                href={`/documents/${d.id}`}
                className="text-sm font-medium hover:underline"
              >
                {d.title ?? "(untitled)"}
              </Link>
              <div className="text-xs text-muted-foreground">
                {d.source_name ?? "Manual"} ·{" "}
                <span title={d.fetched_at}>{relativeTime(d.fetched_at)}</span>
              </div>
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}
