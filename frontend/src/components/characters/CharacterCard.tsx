"use client";

import { VoiceAuditionButton } from "@/components/voice/VoiceAuditionButton";
import { Badge } from "@/components/ui/badge";
import type { Character } from "@/lib/api";

/** A compact preview of a character — personality, speaking style, and a
 * one-click audition of its sample line in its own voice. */
export function CharacterCard({ character }: { character: Character }) {
  const sample =
    character.sample_line || `Hi, I'm ${character.name} — here's today's story.`;
  return (
    <div className="space-y-1.5 rounded-lg border bg-muted/30 p-3">
      <div className="flex items-center gap-2">
        <p className="font-medium">{character.name}</p>
        {character.voice_id ? (
          <>
            <Badge variant="outline">{character.voice_id}</Badge>
            <VoiceAuditionButton voiceId={character.voice_id} text={sample} size="icon-xs" />
          </>
        ) : (
          <Badge variant="outline">no voice</Badge>
        )}
      </div>
      {character.description ? (
        <p className="text-xs text-muted-foreground">{character.description}</p>
      ) : null}
      {character.speaking_style ? (
        <p className="text-xs text-muted-foreground">
          <span className="font-medium">Style:</span> {character.speaking_style}
        </p>
      ) : null}
      {character.sign_off ? (
        <p className="text-xs text-muted-foreground">
          <span className="font-medium">Sign-off:</span> “{character.sign_off}”
        </p>
      ) : null}
    </div>
  );
}
