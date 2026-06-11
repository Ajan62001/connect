"use client";

import { useState } from "react";
import { BookOpenIcon, SearchXIcon } from "lucide-react";

import { DocumentTable } from "@/components/documents/DocumentTable";
import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { Paginator } from "@/components/shared/Paginator";
import { QueryError } from "@/components/shared/QueryError";
import { SearchBar } from "@/components/shared/SearchBar";
import { Skeleton } from "@/components/ui/skeleton";
import { useDocuments } from "@/lib/queries";

const PAGE_SIZE = 25;

function LibrarySkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="flex items-center gap-4">
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-5 w-16" />
        </div>
      ))}
    </div>
  );
}

export default function LibraryPage() {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  const documents = useDocuments({
    ...(query ? { q: query } : {}),
    page,
    page_size: PAGE_SIZE,
  });

  return (
    <>
      <PageHeader
        title="Library"
        description="Every stored document — full-text searchable (FTS5)."
      />

      <SearchBar
        placeholder="Search the corpus…"
        onSearch={(q) => {
          setQuery(q);
          setPage(1);
        }}
      />

      {documents.isPending ? (
        <LibrarySkeleton />
      ) : documents.isError ? (
        <QueryError
          error={documents.error}
          onRetry={() => void documents.refetch()}
        />
      ) : documents.data.items.length === 0 ? (
        query ? (
          <EmptyState
            icon={SearchXIcon}
            title="No documents match"
            description={`Nothing in the corpus matches “${query}”. FTS5 syntax (AND, OR, "phrases") is supported.`}
          />
        ) : (
          <EmptyState
            icon={BookOpenIcon}
            title="The library is empty"
            description="Documents land here as the poller runs or when you add something via “Add to corpus”."
          />
        )
      ) : (
        <>
          <DocumentTable items={documents.data.items} />
          <Paginator
            page={documents.data.page}
            pageSize={documents.data.page_size}
            total={documents.data.total}
            onPageChange={setPage}
          />
        </>
      )}
    </>
  );
}
