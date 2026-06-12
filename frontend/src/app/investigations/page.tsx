"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Loader2Icon, TelescopeIcon } from "lucide-react";
import { toast } from "sonner";

import { AnalysisStatusChip } from "@/components/analysis/VerdictBadge";
import { EntityAutocomplete } from "@/components/entities/EntityAutocomplete";
import { EmptyState } from "@/components/shared/EmptyState";
import { Paginator } from "@/components/shared/Paginator";
import { QueryError } from "@/components/shared/QueryError";
import { ScopeTabs } from "@/components/shared/ScopeTabs";
import { OwnerByline, VisibilityBadge, VisibilityToggle } from "@/components/shared/Visibility";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, matchesDossierScope } from "@/lib/api";
import type {
  DossierScope,
  EntityListItem,
  InvestigationListItem,
  Visibility,
} from "@/lib/api";
import { formatUsd, relativeTime } from "@/lib/format";
import { useCreateInvestigation, useInvestigations, useMe } from "@/lib/queries";

const PAGE_SIZE = 10;

function CountChips({ item }: { item: InvestigationListItem }) {
  const counts = item.counts;
  if (!counts) return null;
  const questionsTotal = counts.questions_open + counts.questions_answered;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {counts.findings > 0 ? (
        <Badge variant="secondary">
          {counts.findings} finding{counts.findings === 1 ? "" : "s"}
        </Badge>
      ) : null}
      {questionsTotal > 0 ? (
        <Badge variant="secondary">
          {counts.questions_answered}/{questionsTotal} questions
        </Badge>
      ) : null}
      {counts.docs_added > 0 ? (
        <Badge variant="secondary">+{counts.docs_added} docs</Badge>
      ) : null}
    </span>
  );
}

function InvestigationRow({ item }: { item: InvestigationListItem }) {
  return (
    <li>
      <Link
        href={`/investigation/${item.id}`}
        className="flex flex-col gap-1.5 rounded-lg px-3 py-3 transition-colors hover:bg-muted/60"
      >
        <p className="line-clamp-2 text-sm font-medium">
          {item.title ?? `Investigation #${item.id}`}
        </p>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <AnalysisStatusChip status={item.status} />
          <Badge variant="outline">{item.input_type}</Badge>
          <VisibilityBadge visibility={item.visibility} />
          {item.visibility === "shared" ? (
            <OwnerByline ownerId={item.owner_id} ownerName={item.owner_name} />
          ) : null}
          <CountChips item={item} />
          {typeof item.cost_usd === "number" ? (
            <span className="tabular-nums">{formatUsd(item.cost_usd)}</span>
          ) : null}
          <span className="ml-auto">
            {item.finished_at
              ? `finished ${relativeTime(item.finished_at)}`
              : `started ${relativeTime(item.created_at)}`}
          </span>
        </div>
      </Link>
    </li>
  );
}

function RecentInvestigations() {
  const [page, setPage] = useState(1);
  // Mine | Shared | All — the community feed lives on this list (design §6).
  const [scope, setScope] = useState<DossierScope>("all");
  const me = useMe();
  const investigations = useInvestigations({ page, page_size: PAGE_SIZE, scope });

  const tabs = (
    <div className="flex items-center justify-between gap-3">
      <h2 className="text-sm font-medium text-muted-foreground">
        Recent investigations
      </h2>
      <ScopeTabs
        value={scope}
        onChange={(next) => {
          setScope(next);
          setPage(1);
        }}
      />
    </div>
  );

  if (investigations.isPending) {
    return (
      <div className="space-y-3">
        {tabs}
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-16 w-full rounded-lg" />
        ))}
      </div>
    );
  }
  if (investigations.isError) {
    // A backend without investigation endpoints yet degrades, not crashes.
    if (
      investigations.error instanceof ApiError &&
      investigations.error.status === 404
    ) {
      return (
        <EmptyState
          icon={TelescopeIcon}
          title="Investigation API not available yet"
          description="The backend hasn't shipped investigation mode; this page will light up once it does."
        />
      );
    }
    return (
      <QueryError
        error={investigations.error}
        onRetry={() => void investigations.refetch()}
      />
    );
  }

  // Client half of the scope contract — a no-op once the backend filters.
  const items = investigations.data.items.filter((item) =>
    matchesDossierScope(item, scope, me.data?.id),
  );

  if (investigations.data.items.length === 0) {
    return (
      <div className="space-y-3">
        {tabs}
        <EmptyState
          icon={TelescopeIcon}
          title={
            scope === "all"
              ? "No investigations yet"
              : `No ${scope === "mine" ? "investigations of yours" : "shared investigations"} yet`
          }
          description="Give the agent a topic above — it works the corpus with a why-mindset and grounds every connection in quotes."
        />
      </div>
    );
  }
  return (
    <div className="space-y-4">
      {tabs}
      {items.length === 0 ? (
        <EmptyState
          icon={TelescopeIcon}
          title={
            scope === "mine"
              ? "None of your investigations on this page"
              : "No shared investigations on this page"
          }
          description="Pagination happens before this filter — try the next page or another tab."
        />
      ) : (
        <ul className="divide-y rounded-xl border bg-card">
          {items.map((item) => (
            <InvestigationRow key={item.id} item={item} />
          ))}
        </ul>
      )}
      <Paginator
        page={investigations.data.page}
        pageSize={investigations.data.page_size}
        total={investigations.data.total}
        onPageChange={setPage}
      />
    </div>
  );
}

export default function InvestigationsPage() {
  const router = useRouter();
  const create = useCreateInvestigation();
  const [topic, setTopic] = useState("");
  const [entity, setEntity] = useState<EntityListItem | null>(null);
  // Shared by default (design §1) — private is the deliberate opt-in.
  const [visibility, setVisibility] = useState<Visibility>("shared");

  // Exactly one seed: a picked entity wins over free text (the autocomplete
  // clears its selection the moment the user edits it again).
  const canSubmit = (entity !== null || topic.trim().length > 0) && !create.isPending;

  const submit = () => {
    if (!canSubmit) return;
    const seed = entity !== null ? { entity_id: entity.id } : { topic: topic.trim() };
    create.mutate({ ...seed, visibility }, {
      onSuccess: (accepted) =>
        router.push(`/investigation/${accepted.investigation_id}`),
      onError: (error) =>
        toast.error("Could not start investigation", {
          description: error.message,
        }),
    });
  };

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <Card className="mt-6">
        <CardHeader className="text-center">
          <CardTitle className="text-lg">Investigate a topic</CardTitle>
          <CardDescription>
            An investigative agent builds an article-grounded understanding:
            why it happened, what it reacts to, what alternatives existed, and
            what remains open.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="Describe the topic to investigate…"
            className="min-h-24"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="space-y-1">
            <EntityAutocomplete
              value={entity}
              onSelect={setEntity}
              placeholder="…or start from an entity"
            />
            {entity !== null ? (
              <p className="text-xs text-muted-foreground">
                Seeding from <span className="font-medium">{entity.name}</span>
                {topic.trim() ? " — the topic text above is ignored" : ""}.
              </p>
            ) : null}
          </div>
          <VisibilityToggle
            value={visibility}
            onChange={setVisibility}
            disabled={create.isPending}
            kind="investigation"
          />
          <div className="flex items-center justify-end gap-3">
            <span className="text-xs text-muted-foreground">
              ~$0.60 typical, $1.00 cap
            </span>
            <Button disabled={!canSubmit} onClick={submit}>
              {create.isPending ? (
                <Loader2Icon className="animate-spin" data-icon="inline-start" />
              ) : (
                <TelescopeIcon data-icon="inline-start" />
              )}
              Investigate
            </Button>
          </div>
        </CardContent>
      </Card>

      <section className="space-y-3">
        <RecentInvestigations />
      </section>
    </div>
  );
}
