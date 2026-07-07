"use client";

import { useState } from "react";

import { Segmented } from "@/components/shared/Segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { ScriptPreset, ScriptPresetInput } from "@/lib/api";

const SCENE_OPTS = [
  { value: "3", label: "3" },
  { value: "4", label: "4" },
  { value: "5", label: "5" },
  { value: "6", label: "6" },
];

/** Create/edit form for a custom script-type preset. */
export function ScriptPresetForm({
  preset,
  submitting,
  submitLabel,
  onSubmit,
}: {
  preset?: ScriptPreset;
  submitting: boolean;
  submitLabel: string;
  onSubmit: (payload: ScriptPresetInput) => void;
}) {
  const [name, setName] = useState(preset?.name ?? "");
  const [guidance, setGuidance] = useState(preset?.guidance ?? "");
  const [sceneCount, setSceneCount] = useState(String(preset?.scene_count ?? 4));

  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <Label>Name</Label>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Hot take" />
      </div>
      <div className="space-y-1">
        <Label>Guidance</Label>
        <p className="text-xs text-muted-foreground">
          How the script should be shaped — hook style, structure, pacing. Form only; the citation
          rules are never relaxed.
        </p>
        <Textarea
          className="min-h-24"
          value={guidance}
          onChange={(e) => setGuidance(e.target.value)}
          placeholder="Spicy opinion, one bold claim up front, rapid three-beat build, land a mic-drop."
        />
      </div>
      <div className="space-y-1">
        <Label>Scenes (reels)</Label>
        <Segmented value={sceneCount} options={SCENE_OPTS} onChange={setSceneCount} />
      </div>
      <Button
        disabled={submitting || !name.trim()}
        onClick={() =>
          onSubmit({
            name: name.trim(),
            guidance: guidance.trim(),
            scene_count: Number(sceneCount),
          })
        }
      >
        {submitLabel}
      </Button>
    </div>
  );
}
