"use client";

import type { VoiceOption } from "@/lib/api";
import { useVoices } from "@/lib/queries";

const selectCls = "h-8 rounded-md border bg-background px-2 text-sm";

/** The reel-narration voice select (ElevenLabs + 'vb:' voicebox voices). The
 * null option label is contextual — "Default voice" globally, "Channel voice"
 * inside a workspace where the backend resolves a channel default. */
export function VoicePicker({
  value,
  onChange,
  defaultLabel = "Default voice",
}: {
  value: string | null;
  onChange: (v: string | null) => void;
  defaultLabel?: string;
}) {
  const voices = useVoices();
  return (
    <select
      className={selectCls}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">{defaultLabel}</option>
      {(voices.data ?? []).map((v: VoiceOption) => (
        <option key={v.id} value={v.id}>
          {v.name}
          {v.description ? ` — ${v.description}` : ""}
        </option>
      ))}
    </select>
  );
}
