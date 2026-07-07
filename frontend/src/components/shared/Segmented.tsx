"use client";

import { Button } from "@/components/ui/button";

/** A small inline segmented control (a row of pill buttons). Extracted from
 * PostSettingsDialog so the channel settings sections can reuse it. */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((o) => (
        <Button
          key={o.value}
          type="button"
          size="sm"
          variant={value === o.value ? "secondary" : "outline"}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  );
}
