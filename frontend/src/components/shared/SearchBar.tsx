"use client";

import { useEffect, useRef, useState } from "react";
import { SearchIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * Controlled-ish search input: keeps its own text state and notifies the
 * parent with `onSearch` after a debounce (default 300ms).
 */
export function SearchBar({
  placeholder = "Search…",
  initialValue = "",
  debounceMs = 300,
  onSearch,
}: {
  placeholder?: string;
  initialValue?: string;
  debounceMs?: number;
  onSearch: (query: string) => void;
}) {
  const [value, setValue] = useState(initialValue);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onSearchRef = useRef(onSearch);

  useEffect(() => {
    onSearchRef.current = onSearch;
  }, [onSearch]);

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  function schedule(next: string) {
    setValue(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => onSearchRef.current(next.trim()), debounceMs);
  }

  function clear() {
    if (timer.current) clearTimeout(timer.current);
    setValue("");
    onSearchRef.current("");
  }

  return (
    <div className="relative max-w-md flex-1">
      <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={value}
        onChange={(event) => schedule(event.target.value)}
        placeholder={placeholder}
        className="pl-8 pr-8"
        aria-label="Search"
      />
      {value ? (
        <Button
          variant="ghost"
          size="icon-xs"
          className="absolute top-1/2 right-1.5 -translate-y-1/2"
          onClick={clear}
          aria-label="Clear search"
        >
          <XIcon />
        </Button>
      ) : null}
    </div>
  );
}
