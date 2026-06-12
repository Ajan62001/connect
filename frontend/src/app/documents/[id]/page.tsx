"use client";

import { use, useState } from "react";
import {
  ArrowLeftIcon,
  ExternalLinkIcon,
  FileQuestionIcon,
  Loader2Icon,
  Share2Icon,
} from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import { EntityPill } from "@/components/entities/EntityPill";
import { StatementsCard } from "@/components/views/StatementsCard";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { ShareDocumentDialog } from "@/components/shared/ShareDialog";
import { StatusChip, WatchHitChip } from "@/components/shared/StatusChip";
import { OwnerByline, VisibilityBadge } from "@/components/shared/Visibility";
import { EmptyState } from "@/components/shared/EmptyState";
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
import type {
  Document,
  DocumentEnrichment,
  DocumentLink,
  DocumentLinkStatus,
  EnrichmentClaim,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  absoluteTime,
  mediaTypeLabel,
  relativeTime,
  shortHash,
  urlHost,
} from "@/lib/format";
import { useDocument, useFetchDocumentLink, useMe } from "@/lib/queries";

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-0.5">
      <dt className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
        {label}
      </dt>
      <dd className="text-sm break-words">{children}</dd>
    </div>
  );
}

const LINK_STATUS_STYLES: Record<
  DocumentLinkStatus,
  { label: string; className: string }
> = {
  not_followed: {
    label: "not followed",
    className: "border-border bg-transparent text-muted-foreground",
  },
  pending: {
    label: "pending",
    className: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  },
  fetched: {
    label: "fetched",
    className:
      "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  },
  failed: {
    label: "failed",
    className: "bg-destructive/10 text-destructive dark:bg-destructive/20",
  },
  skipped: {
    label: "skipped",
    className: "border-border bg-transparent text-muted-foreground",
  },
};

function LinkStatusChip({ status }: { status: DocumentLinkStatus }) {
  const style = LINK_STATUS_STYLES[status] ?? {
    label: status,
    className: "bg-muted text-muted-foreground",
  };
  return (
    <Badge variant="secondary" className={style.className}>
      {style.label}
    </Badge>
  );
}

/** Anchor text, falling back to the URL's path tail. */
function linkLabel(link: DocumentLink): string {
  if (link.anchor_text) return link.anchor_text;
  try {
    const url = new URL(link.url);
    const segments = url.pathname.split("/").filter(Boolean);
    return segments[segments.length - 1] ?? url.hostname;
  } catch {
    return link.url;
  }
}

function LinkRow({
  link,
  onFetch,
  isFetching,
}: {
  link: DocumentLink;
  onFetch: (linkId: number) => void;
  isFetching: boolean;
}) {
  const label = linkLabel(link);
  const canFetch = link.status === "not_followed" || link.status === "failed";
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5">
      <span className="min-w-0 flex-1">
        {link.status === "fetched" && link.resolved_document_id !== null ? (
          <Link
            href={`/documents/${link.resolved_document_id}`}
            className="text-sm font-medium text-primary underline-offset-4 hover:underline"
            title={link.url}
          >
            {label}
          </Link>
        ) : (
          <span className="text-sm font-medium" title={link.url}>
            {label}
          </span>
        )}
        <span className="ml-2 text-xs text-muted-foreground">
          {urlHost(link.url)}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {link.is_file ? <Badge variant="outline">file</Badge> : null}
        {link.is_official ? (
          <Badge
            variant="secondary"
            className="bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
          >
            official
          </Badge>
        ) : null}
        <LinkStatusChip status={link.status} />
        {canFetch ? (
          <Button
            variant="outline"
            size="xs"
            disabled={isFetching}
            onClick={() => onFetch(link.id)}
          >
            {isFetching ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : null}
            {link.status === "failed" ? "Retry" : "Fetch"}
          </Button>
        ) : null}
      </span>
      {link.status === "failed" && link.error ? (
        <span
          className={cn("w-full truncate text-xs text-destructive")}
          title={link.error}
        >
          {link.error}
        </span>
      ) : null}
    </li>
  );
}

/** Claim row: text plus a small bar for the model's check-worthiness score. */
function ClaimRow({ claim }: { claim: EnrichmentClaim }) {
  const score = Math.min(Math.max(claim.check_worthiness, 0), 1);
  return (
    <li className="flex items-start justify-between gap-4 py-2">
      <span className="min-w-0 flex-1 text-sm">{claim.text}</span>
      <span
        className="mt-1.5 flex shrink-0 items-center gap-1.5"
        title={`Check-worthiness ${(score * 100).toFixed(0)}%`}
      >
        <span className="h-1.5 w-14 overflow-hidden rounded-full bg-muted">
          <span
            className="block h-full rounded-full bg-amber-500/80"
            style={{ width: `${Math.round(score * 100)}%` }}
          />
        </span>
      </span>
    </li>
  );
}

/** T1 enrichment payload: summary, event type, topics, entities, claims. */
function EnrichmentCard({ enrichment }: { enrichment: DocumentEnrichment }) {
  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          Enrichment
          <Badge variant="outline">{enrichment.event_type.replace(/_/g, " ")}</Badge>
        </CardTitle>
        <CardDescription title={absoluteTime(enrichment.created_at)}>
          {enrichment.model} · {relativeTime(enrichment.created_at)}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="max-w-prose text-sm leading-6">{enrichment.summary}</p>

        {enrichment.topics.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {enrichment.topics.map((topic) => (
              <Badge key={topic} variant="secondary">
                {topic}
              </Badge>
            ))}
          </div>
        ) : null}

        {enrichment.entities.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {enrichment.entities.map((entity) => (
              <EntityPill key={entity.id} entity={entity} />
            ))}
          </div>
        ) : null}

        {enrichment.claims.length > 0 ? (
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Claims
            </p>
            <ul className="mt-1 divide-y">
              {enrichment.claims.map((claim) => (
                <ClaimRow key={claim.id} claim={claim} />
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function LinksCard({ document }: { document: Document }) {
  const fetchLink = useFetchDocumentLink(document.id);

  // `?? []` tolerates a backend that predates link extraction.
  const links = document.links ?? [];
  if (links.length === 0) return null;

  const onFetch = (linkId: number) => {
    fetchLink.mutate(linkId, {
      onSuccess: (result) => {
        if (result.link.status === "fetched") {
          toast.success("Link fetched", {
            description: result.document?.title ?? result.link.url,
          });
        } else {
          toast.error("Fetch failed", {
            description: result.link.error ?? result.link.url,
          });
        }
      },
      onError: (error) =>
        toast.error("Fetch failed", { description: error.message }),
    });
  };

  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Links in this document</CardTitle>
        <CardDescription>
          In-content links extracted from the stored page snapshot.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y">
          {links.map((link) => (
            <LinkRow
              key={link.id}
              link={link}
              onFetch={onFetch}
              isFetching={
                fetchLink.isPending && fetchLink.variables === link.id
              }
            />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

/**
 * Visibility row + share affordance (tenancy Phase C). Uploads/pasted text
 * default private (design §1); the owner shares from here. Renders nothing
 * against a pre-tenancy backend (no `visibility` field).
 */
function VisibilityRow({ document }: { document: Document }) {
  const me = useMe();
  const [shareOpen, setShareOpen] = useState(false);

  if (!document.visibility) return null;
  const canShare =
    document.visibility === "private" &&
    document.owner_id != null &&
    document.owner_id === me.data?.id;

  return (
    <MetaRow label="Visibility">
      <span className="flex flex-wrap items-center gap-1.5">
        <VisibilityBadge visibility={document.visibility} showShared />
        {/* Documents carry owner_id only (no display name on this row). */}
        <OwnerByline ownerId={document.owner_id} />
        {canShare ? (
          <Button variant="outline" size="xs" onClick={() => setShareOpen(true)}>
            <Share2Icon data-icon="inline-start" />
            Share
          </Button>
        ) : null}
      </span>
      {document.visibility === "private" ? (
        <p className="mt-1 text-xs leading-snug text-muted-foreground">
          Only you can see this document. Share it to make it readable by
          every member and eligible for knowledge extraction.
        </p>
      ) : null}
      <ShareDocumentDialog
        id={document.id}
        title={document.title}
        open={shareOpen}
        onOpenChange={setShareOpen}
      />
    </MetaRow>
  );
}

function MetadataSidebar({ document }: { document: Document }) {
  return (
    <aside className="w-full shrink-0 lg:w-64">
      <dl className="space-y-4 rounded-xl border bg-card p-4">
        <MetaRow label="Source">{document.source_name ?? "Manual ingest"}</MetaRow>
        <VisibilityRow document={document} />
        {document.event ? (
          <MetaRow label="Event">
            <Badge
              variant="secondary"
              render={<Link href={`/event/${document.event.id}`} />}
              className="max-w-full"
            >
              <span className="truncate">{document.event.title}</span>
            </Badge>
          </MetaRow>
        ) : null}
        {document.url ? (
          <MetaRow label="URL">
            <a
              href={document.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
            >
              {urlHost(document.url)}
              <ExternalLinkIcon className="size-3.5 shrink-0" aria-hidden />
            </a>
          </MetaRow>
        ) : null}
        {document.published_at ? (
          <MetaRow label="Published">{absoluteTime(document.published_at)}</MetaRow>
        ) : null}
        <MetaRow label="Fetched">{absoluteTime(document.fetched_at)}</MetaRow>
        {document.author ? <MetaRow label="Author">{document.author}</MetaRow> : null}
        {document.language ? (
          <MetaRow label="Language">{document.language}</MetaRow>
        ) : null}
        {document.media_type ? (
          <MetaRow label="Media type">{mediaTypeLabel(document.media_type)}</MetaRow>
        ) : null}
        <MetaRow label="Content hash">
          <code className="font-mono text-xs" title={document.content_hash}>
            {shortHash(document.content_hash)}
          </code>
        </MetaRow>
        <MetaRow label="Enrichment">
          <span className="flex flex-wrap items-center gap-1.5">
            <StatusChip status={document.enrichment_status} />
            <span className="text-xs text-muted-foreground">
              tier {document.enrichment_tier}
            </span>
            {document.watch_hit ? <WatchHitChip /> : null}
          </span>
        </MetaRow>
        {document.canonical_document_id !== null ? (
          <MetaRow label="Duplicate of">
            <Link
              href={`/documents/${document.canonical_document_id}`}
              className="text-primary underline-offset-4 hover:underline"
            >
              Document #{document.canonical_document_id}
            </Link>
          </MetaRow>
        ) : null}
        {(document.linked_from ?? []).length > 0 ? (
          <MetaRow label="Discovered via">
            <span className="flex flex-col gap-1">
              {document.linked_from.map((parent) => (
                <Link
                  key={parent.document_id}
                  href={`/documents/${parent.document_id}`}
                  className="text-primary underline-offset-4 hover:underline"
                >
                  {parent.title ?? `Document #${parent.document_id}`}
                </Link>
              ))}
            </span>
          </MetaRow>
        ) : null}
      </dl>
    </aside>
  );
}

function DocumentSkeleton() {
  return (
    <div className="flex flex-col gap-6 lg:flex-row">
      <div className="flex-1 space-y-3">
        <Skeleton className="h-7 w-3/4" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-2/3" />
      </div>
      <Skeleton className="h-64 w-full lg:w-64" />
    </div>
  );
}

export default function DocumentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const documentId = Number(id);
  const document = useDocument(documentId);

  if (!Number.isFinite(documentId)) {
    return (
      <EmptyState
        icon={FileQuestionIcon}
        title="Invalid document id"
        description={`“${id}” is not a document id.`}
      />
    );
  }

  return (
    <>
      <Button variant="ghost" size="sm" render={<Link href="/library" />}>
        <ArrowLeftIcon data-icon="inline-start" />
        Library
      </Button>

      {document.isPending ? (
        <DocumentSkeleton />
      ) : document.isError ? (
        <QueryError
          error={document.error}
          onRetry={() => void document.refetch()}
        />
      ) : (
        <>
          <PageHeader title={document.data.title ?? "(untitled document)"} />
          <div className="flex flex-col gap-6 lg:flex-row">
            <article className="min-w-0 flex-1">
              {document.data.enrichment ? (
                <EnrichmentCard enrichment={document.data.enrichment} />
              ) : null}
              {/* `?? []` tolerates a backend that predates schema v9. */}
              <StatementsCard statements={document.data.statements ?? []} />
              {document.data.content_text ? (
                <div className="max-w-prose text-[0.95rem] leading-7 whitespace-pre-wrap">
                  {document.data.content_text}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No extracted text for this document.
                </p>
              )}
              <LinksCard document={document.data} />
            </article>
            <MetadataSidebar document={document.data} />
          </div>
        </>
      )}
    </>
  );
}
