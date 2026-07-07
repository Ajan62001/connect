"use client";

import { useCharacters } from "@/lib/queries";

const selectCls = "h-8 rounded-md border bg-background px-2 text-sm";

/** Pick a character from the shared roster. ``nullLabel`` distinguishes the
 * settings context ("No character") from the per-reel one ("Channel default"). */
export function CharacterPicker({
  value,
  onChange,
  nullLabel = "No character",
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  nullLabel?: string;
}) {
  const characters = useCharacters();
  return (
    <select
      className={selectCls}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
    >
      <option value="">{nullLabel}</option>
      {(characters.data ?? []).map((c) => (
        <option key={c.id} value={c.id}>
          {c.name}
        </option>
      ))}
    </select>
  );
}
