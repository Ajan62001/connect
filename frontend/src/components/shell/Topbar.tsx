"use client";

import { useState } from "react";
import { PlusIcon } from "lucide-react";

import { IngestDialog } from "@/components/ingest/IngestDialog";
import { SpendBadge } from "@/components/shell/SpendBadge";
import { UserMenu } from "@/components/shell/UserMenu";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import { useHealth } from "@/lib/queries";

function HealthDot() {
  const health = useHealth();

  const status = health.isError
    ? { className: "bg-destructive", label: "API offline" }
    : health.data?.ok
      ? {
          className: "bg-emerald-500",
          label: `API ok · schema v${health.data.schema_version} · vectors: ${health.data.vector_backend}`,
        }
      : { className: "bg-muted-foreground/40", label: "API status unknown" };

  return (
    <span
      className="flex items-center gap-2 text-xs text-muted-foreground"
      title={status.label}
    >
      <span className={cn("size-2 rounded-full", status.className)} aria-hidden />
      <span className="hidden sm:inline">{status.label}</span>
    </span>
  );
}

export function Topbar() {
  const [ingestOpen, setIngestOpen] = useState(false);

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-background px-6">
      <HealthDot />
      <div className="flex items-center gap-3">
        <SpendBadge />
        <Button onClick={() => setIngestOpen(true)}>
          <PlusIcon data-icon="inline-start" />
          Add to corpus
        </Button>
        <Separator orientation="vertical" className="h-6" />
        <UserMenu />
      </div>
      <IngestDialog open={ingestOpen} onOpenChange={setIngestOpen} />
    </header>
  );
}
