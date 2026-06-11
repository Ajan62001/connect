"use client";

import { useState } from "react";
import { SearchIcon, SearchXIcon } from "lucide-react";

import { DocumentTable } from "@/components/documents/DocumentTable";
import { EntityPill } from "@/components/entities/EntityPill";
import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
import { SearchBar } from "@/components/shared/SearchBar";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { EntityListItem, SearchKind, SearchResult } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useSearch } from "@/lib/queries";

const KIND_TABS: { value: SearchKind; label: string }[] = [
  { value: "all", label: "All" },
  { value: "documents", label: "Documents" },
  { value: "entities", label: "Entities" },
];

function EntityRow({ entity }: { entity: EntityListItem }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-xl border bg-card px-4 py-2.5">
      <EntityPill entity={entity} className="text-sm" />
      <p className="shrink-0 text-xs text-muted-foreground">
        {entity.mention_count} mention{entity.mention_count === 1 ? "" : "s"} ·{" "}
        {entity.document_count} doc{entity.document_count === 1 ? "" : "s"}
        {entity.last_seen_at
          ? ` · last seen ${relativeTime(entity.last_seen_at)}`
          : ""}
      </p>
    </li>
  );
}

function SearchSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full rounded-xl" />
      ))}
    </div>
  );
}

function Results({ data, kind }: { data: SearchResult; kind: SearchKind }) {
  const entities = data.entities ?? [];
  const documents = data.documents ?? [];
  const showEntities = kind !== "documents";
  const showDocuments = kind !== "entities";

  const nothing =
    (!showEntities || entities.length === 0) &&
    (!showDocuments || documents.length === 0);
  if (nothing) {
    return (
      <EmptyState
        icon={SearchXIcon}
        title="No matches"
        description="Nothing in the corpus matches this query. FTS5 syntax (AND, OR, “phrases”) is supported."
      />
    );
  }

  return (
    <div className="space-y-6">
      {showEntities && entities.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold tracking-tight">
            Entities{" "}
            <span className="font-normal text-muted-foreground">
              {data.total_entities}
            </span>
          </h2>
          <ul className="space-y-2">
            {entities.map((entity) => (
              <EntityRow key={entity.id} entity={entity} />
            ))}
          </ul>
        </section>
      ) : null}

      {showDocuments && documents.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold tracking-tight">
            Documents{" "}
            <span className="font-normal text-muted-foreground">
              {data.total_documents}
            </span>
          </h2>
          <DocumentTable items={documents} />
        </section>
      ) : null}
    </div>
  );
}

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<SearchKind>("all");

  const search = useSearch(query, kind);

  return (
    <>
      <PageHeader
        title="Search"
        description="One query across documents (FTS5) and entities (name or alias)."
      />

      <div className="flex flex-wrap items-center gap-4">
        <SearchBar
          placeholder="Search documents and entities…"
          onSearch={setQuery}
        />
        <Tabs
          value={kind}
          onValueChange={(value) => setKind(value as SearchKind)}
        >
          <TabsList>
            {KIND_TABS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                {tab.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

      {!query.trim() ? (
        <EmptyState
          icon={SearchIcon}
          title="Search the corpus"
          description="Documents match on full text; entities match on name or alias."
        />
      ) : search.isPending ? (
        <SearchSkeleton />
      ) : search.isError ? (
        <QueryError error={search.error} onRetry={() => void search.refetch()} />
      ) : (
        <Results data={search.data} kind={kind} />
      )}
    </>
  );
}
