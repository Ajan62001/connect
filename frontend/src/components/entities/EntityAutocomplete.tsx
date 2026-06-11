"use client";

import { useEffect, useRef, useState } from "react";

import { EntityTypeChip } from "@/components/entities/EntityPill";
import { Input } from "@/components/ui/input";
import type { EntityListItem } from "@/lib/api";
import { useEntities } from "@/lib/queries";

const DEBOUNCE_MS = 250;
const RESULT_LIMIT = 8;

/**
 * Debounced entity picker fed by GET /api/entities?q=. Typing clears any
 * previous selection; picking a row commits it and fills the input with the
 * entity's name.
 */
export function EntityAutocomplete({
  value,
  onSelect,
  placeholder = "Search entities by name or alias…",
}: {
  value: EntityListItem | null;
  onSelect: (entity: EntityListItem | null) => void;
  placeholder?: string;
}) {
  const [text, setText] = useState(value?.name ?? "");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  const active = open && query.trim().length > 0;
  const results = useEntities(
    { q: query, page_size: RESULT_LIMIT },
    { enabled: active },
  );

  function onChange(next: string) {
    setText(next);
    onSelect(null); // editing the text invalidates the previous selection
    setOpen(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setQuery(next.trim()), DEBOUNCE_MS);
  }

  function choose(entity: EntityListItem) {
    onSelect(entity);
    setText(entity.name);
    setOpen(false);
  }

  return (
    <div className="relative">
      <Input
        value={text}
        onChange={(event) => onChange(event.target.value)}
        onBlur={() => setOpen(false)}
        placeholder={placeholder}
        aria-label="Search entities"
        autoComplete="off"
      />
      {active ? (
        <div className="absolute z-50 mt-1 w-full overflow-hidden rounded-lg bg-popover shadow-md ring-1 ring-foreground/10">
          {results.isPending ? (
            <p className="px-3 py-2 text-xs text-muted-foreground">Searching…</p>
          ) : results.isError ? (
            <p className="px-3 py-2 text-xs text-destructive">
              Entity search failed: {results.error.message}
            </p>
          ) : results.data.items.length === 0 ? (
            <p className="px-3 py-2 text-xs text-muted-foreground">
              No entities match “{query}”. Entities appear as enrichment runs.
            </p>
          ) : (
            <ul className="max-h-64 overflow-y-auto py-1">
              {results.data.items.map((entity) => (
                <li key={entity.id}>
                  <button
                    type="button"
                    className="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-sm hover:bg-accent hover:text-accent-foreground"
                    // mousedown so the pick lands before the input's blur.
                    onMouseDown={(event) => {
                      event.preventDefault();
                      choose(entity);
                    }}
                  >
                    <span className="truncate font-medium">{entity.name}</span>
                    <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                      {entity.document_count} doc
                      {entity.document_count === 1 ? "" : "s"}
                      <EntityTypeChip type={entity.entity_type} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
