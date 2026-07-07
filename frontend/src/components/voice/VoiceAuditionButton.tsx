"use client";

import { useState } from "react";
import { Loader2Icon, PlayIcon, SquareIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { auditionVoice } from "@/lib/api";

// One shared <audio> element so starting a clip stops any other; a per-key
// object-URL cache so replays don't re-hit TTS.
let current: HTMLAudioElement | null = null;
const cache = new Map<string, string>();

/** A play/stop button that auditions a voice (voicebox or ElevenLabs). Disabled
 * when there is no voice to play. */
export function VoiceAuditionButton({
  voiceId,
  text,
  size = "icon-sm",
}: {
  voiceId: string | null | undefined;
  text?: string;
  size?: "icon-xs" | "icon-sm";
}) {
  const [state, setState] = useState<"idle" | "loading" | "playing">("idle");

  const stop = () => {
    if (current) {
      current.pause();
      current = null;
    }
    setState("idle");
  };

  const play = async () => {
    if (!voiceId) return;
    if (state === "playing") return stop();
    if (current) stop();
    setState("loading");
    try {
      const key = `${voiceId}::${text ?? ""}`;
      let url = cache.get(key);
      if (!url) {
        url = await auditionVoice({ voice_id: voiceId, text });
        cache.set(key, url);
      }
      const audio = new Audio(url);
      current = audio;
      audio.onended = () => {
        if (current === audio) current = null;
        setState("idle");
      };
      await audio.play();
      setState("playing");
    } catch (e) {
      setState("idle");
      toast.error("Could not play voice", {
        description: e instanceof Error ? e.message : String(e),
      });
    }
  };

  const Icon = state === "loading" ? Loader2Icon : state === "playing" ? SquareIcon : PlayIcon;
  return (
    <Button
      type="button"
      variant="ghost"
      size={size}
      disabled={!voiceId || state === "loading"}
      aria-label="Audition voice"
      onClick={play}
    >
      <Icon className={state === "loading" ? "animate-spin" : undefined} />
    </Button>
  );
}
