"use client";

import { useState } from "react";

import { VoiceAuditionButton } from "@/components/voice/VoiceAuditionButton";
import { VoicePicker } from "@/components/voice/VoicePicker";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { Character, CharacterInput } from "@/lib/api";

/** Create/edit form for a roster character. Plain controlled state (the
 * PostSettingsDialog style) — no react-hook-form dependency. */
export function CharacterForm({
  character,
  submitting,
  submitLabel,
  onSubmit,
}: {
  character?: Character;
  submitting: boolean;
  submitLabel: string;
  onSubmit: (payload: CharacterInput) => void;
}) {
  const [name, setName] = useState(character?.name ?? "");
  const [description, setDescription] = useState(character?.description ?? "");
  const [speakingStyle, setSpeakingStyle] = useState(character?.speaking_style ?? "");
  const [sampleLine, setSampleLine] = useState(character?.sample_line ?? "");
  const [signOff, setSignOff] = useState(character?.sign_off ?? "");
  const [voiceId, setVoiceId] = useState<string | null>(character?.voice_id ?? null);
  const [catchphrases, setCatchphrases] = useState((character?.catchphrases ?? []).join("\n"));

  const submit = () =>
    onSubmit({
      name: name.trim(),
      description: description.trim(),
      speaking_style: speakingStyle.trim(),
      sample_line: sampleLine.trim(),
      sign_off: signOff.trim(),
      voice_id: voiceId,
      catchphrases: catchphrases
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 8),
    });

  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <Label>Name</Label>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Bantu the Owl" />
      </div>
      <div className="space-y-1">
        <Label>Personality</Label>
        <Textarea
          className="min-h-16"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="A wise, sardonic Delhi news anchor who's seen it all."
        />
      </div>
      <div className="space-y-1">
        <Label>Speaking style</Label>
        <Textarea
          className="min-h-16"
          value={speakingStyle}
          onChange={(e) => setSpeakingStyle(e.target.value)}
          placeholder="Fast, punchy, a little cheeky; drops a rhetorical question mid-scene."
        />
      </div>
      <div className="space-y-1">
        <Label>Voice</Label>
        <div className="flex items-center gap-1.5">
          <VoicePicker value={voiceId} onChange={setVoiceId} defaultLabel="No voice" />
          <VoiceAuditionButton voiceId={voiceId} text={sampleLine || `Hi, I'm ${name || "your host"}.`} />
        </div>
      </div>
      <div className="space-y-1">
        <Label>Sample line (for auditions)</Label>
        <Input
          value={sampleLine}
          onChange={(e) => setSampleLine(e.target.value)}
          placeholder="Breaking tonight — and you won't believe who's involved."
        />
      </div>
      <div className="space-y-1">
        <Label>Sign-off</Label>
        <Input value={signOff} onChange={(e) => setSignOff(e.target.value)} placeholder="Stay sharp." />
      </div>
      <div className="space-y-1">
        <Label>Catchphrases (one per line, up to 8)</Label>
        <Textarea
          className="min-h-16"
          value={catchphrases}
          onChange={(e) => setCatchphrases(e.target.value)}
          placeholder={"Let's break it down\nHere's the twist"}
        />
      </div>
      <Button disabled={submitting || !name.trim()} onClick={submit}>
        {submitLabel}
      </Button>
    </div>
  );
}
