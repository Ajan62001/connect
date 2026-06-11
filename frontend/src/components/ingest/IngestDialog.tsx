"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import { useDropzone } from "react-dropzone";
import { FileTextIcon, Loader2Icon, UploadIcon, XIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { useIngest, type IngestInput } from "@/lib/queries";

/**
 * Shared "+ Add to corpus" dialog: paste text, submit a URL, or upload a
 * PDF/txt file. On success navigates to the stored document's reading view.
 */
export function IngestDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const ingest = useIngest();

  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);

  const onDrop = useCallback((accepted: File[]) => {
    if (accepted.length > 0) setFile(accepted[0]);
  }, []);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    multiple: false,
    accept: {
      "application/pdf": [".pdf"],
      "text/plain": [".txt"],
    },
  });

  function reset() {
    setText("");
    setTitle("");
    setUrl("");
    setFile(null);
    ingest.reset();
  }

  function submit(input: IngestInput) {
    ingest.mutate(input, {
      onSuccess: ({ document, deduped }) => {
        toast.success(deduped ? "Already in corpus" : "Stored", {
          description: document.title ?? undefined,
        });
        onOpenChange(false);
        reset();
        router.push(`/documents/${document.id}`);
      },
      onError: (error) => {
        toast.error("Ingestion failed", { description: error.message });
      },
    });
  }

  const busy = ingest.isPending;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!busy) onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add to corpus</DialogTitle>
          <DialogDescription>
            Stored as an immutable snapshot, deduplicated, indexed for search.
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="text">
          <TabsList className="w-full">
            <TabsTrigger value="text">Text</TabsTrigger>
            <TabsTrigger value="url">URL</TabsTrigger>
            <TabsTrigger value="file">File</TabsTrigger>
          </TabsList>

          <TabsContent value="text" className="space-y-3 pt-2">
            <div className="space-y-1.5">
              <Label htmlFor="ingest-title">Title (optional)</Label>
              <Input
                id="ingest-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="e.g. RBI circular on priority sector lending"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ingest-text">Text</Label>
              <Textarea
                id="ingest-text"
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder="Paste an article, press release, or any text…"
                className="min-h-40"
              />
            </div>
            <Button
              className="w-full"
              disabled={busy || text.trim().length === 0}
              onClick={() =>
                submit({
                  mode: "text",
                  payload: {
                    text: text.trim(),
                    ...(title.trim() ? { title: title.trim() } : {}),
                  },
                })
              }
            >
              {busy ? <Loader2Icon className="animate-spin" /> : null}
              Store text
            </Button>
          </TabsContent>

          <TabsContent value="url" className="space-y-3 pt-2">
            <div className="space-y-1.5">
              <Label htmlFor="ingest-url">URL</Label>
              <Input
                id="ingest-url"
                type="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://pib.gov.in/PressReleasePage.aspx?PRID=…"
              />
            </div>
            <Button
              className="w-full"
              disabled={busy || !/^https?:\/\/\S+/.test(url.trim())}
              onClick={() => submit({ mode: "url", url: url.trim() })}
            >
              {busy ? <Loader2Icon className="animate-spin" /> : null}
              Fetch and store
            </Button>
          </TabsContent>

          <TabsContent value="file" className="space-y-3 pt-2">
            <div
              {...getRootProps()}
              className={cn(
                "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-10 text-center transition-colors",
                isDragActive
                  ? "border-primary bg-primary/5"
                  : "hover:border-muted-foreground/50",
              )}
            >
              <input {...getInputProps()} />
              <UploadIcon className="size-6 text-muted-foreground" aria-hidden />
              <p className="text-sm text-muted-foreground">
                {isDragActive
                  ? "Drop the file here…"
                  : "Drag a PDF or .txt here, or click to browse"}
              </p>
            </div>
            {file ? (
              <div className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm">
                <span className="flex min-w-0 items-center gap-2">
                  <FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{file.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {(file.size / 1024).toFixed(0)} KB
                  </span>
                </span>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  onClick={() => setFile(null)}
                  aria-label="Remove file"
                >
                  <XIcon />
                </Button>
              </div>
            ) : null}
            <Button
              className="w-full"
              disabled={busy || file === null}
              onClick={() => {
                if (file) submit({ mode: "file", file });
              }}
            >
              {busy ? <Loader2Icon className="animate-spin" /> : null}
              Upload and store
            </Button>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
