"use client";

import { useMemo, useState } from "react";
import { CheckIcon, Loader2Icon, PlusIcon, XIcon } from "lucide-react";
import { toast } from "sonner";

import { VoiceAuditionButton } from "@/components/voice/VoiceAuditionButton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { VoiceboxPreset } from "@/lib/api";
import {
  useAddVoiceboxProfile,
  useDeleteVoiceboxProfile,
  useVoiceboxCatalog,
  useVoiceboxProfiles,
} from "@/lib/queries";

/** Browse the local voicebox library: your saved profiles (removable) and the
 * Kokoro catalog (filter by language — Hindi prominent — audition + add). */
export function VoiceLibraryDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const profiles = useVoiceboxProfiles();
  const catalog = useVoiceboxCatalog();
  const add = useAddVoiceboxProfile();
  const remove = useDeleteVoiceboxProfile();
  const [query, setQuery] = useState("");
  const [lang, setLang] = useState<string | null>(null);

  const languages = useMemo(() => {
    const set = new Set<string>();
    for (const v of catalog.data ?? []) if (v.language) set.add(v.language);
    return [...set].sort();
  }, [catalog.data]);

  const addedPresetIds = useMemo(
    () => new Set((profiles.data ?? []).map((p) => p.preset_voice_id).filter(Boolean)),
    [profiles.data],
  );

  const filtered = (catalog.data ?? []).filter((v) => {
    if (lang && v.language !== lang) return false;
    if (query && !(v.name ?? v.voice_id).toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  });

  const addPreset = (v: VoiceboxPreset) =>
    add.mutate(
      { name: v.name ?? v.voice_id, preset_voice_id: v.voice_id, preset_engine: "kokoro", language: v.language },
      {
        onSuccess: () => toast.success(`Added ${v.name ?? v.voice_id}`),
        onError: (e) => toast.error("Could not add voice", { description: e.message }),
      },
    );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Voice library</DialogTitle>
          <DialogDescription>
            Add voices from the local Kokoro catalog — including Indian/Hindi voices — then pick
            one as a channel or character voice.
          </DialogDescription>
        </DialogHeader>

        {catalog.isError ? (
          <p className="py-6 text-sm text-muted-foreground">
            Voicebox is not configured on this server.
          </p>
        ) : (
          <div className="space-y-4">
            {/* saved profiles */}
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground">Your voices</p>
              {(profiles.data ?? []).length === 0 ? (
                <p className="text-xs text-muted-foreground">No profiles yet — add one below.</p>
              ) : (
                <ul className="divide-y rounded-md border">
                  {(profiles.data ?? []).map((p) => (
                    <li key={p.id} className="flex items-center gap-2 px-2 py-1.5 text-sm">
                      <span className="flex-1 truncate">{p.name}</span>
                      {p.language ? <Badge variant="outline">{p.language}</Badge> : null}
                      <VoiceAuditionButton voiceId={`vb:${p.id}`} />
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label="Remove voice"
                        onClick={() =>
                          remove.mutate(p.id, {
                            onError: (e) => toast.error("Could not remove", { description: e.message }),
                          })
                        }
                      >
                        <XIcon />
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* catalog */}
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-xs font-medium text-muted-foreground">Kokoro catalog</p>
                <Input
                  className="h-7 w-40"
                  placeholder="Search…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
                <div className="flex flex-wrap gap-1">
                  <Badge
                    variant={lang === null ? "secondary" : "outline"}
                    className="cursor-pointer"
                    onClick={() => setLang(null)}
                  >
                    All
                  </Badge>
                  {languages.map((l) => (
                    <Badge
                      key={l}
                      variant={lang === l ? "secondary" : "outline"}
                      className="cursor-pointer"
                      onClick={() => setLang(l)}
                    >
                      {l}
                    </Badge>
                  ))}
                </div>
              </div>
              <ul className="max-h-64 divide-y overflow-y-auto rounded-md border">
                {filtered.map((v) => {
                  const added = addedPresetIds.has(v.voice_id);
                  return (
                    <li key={v.voice_id} className="flex items-center gap-2 px-2 py-1.5 text-sm">
                      <span className="flex-1 truncate">{v.name ?? v.voice_id}</span>
                      {v.gender ? <Badge variant="outline">{v.gender}</Badge> : null}
                      {v.language ? <Badge variant="outline">{v.language}</Badge> : null}
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        disabled={added || add.isPending}
                        aria-label="Add voice"
                        onClick={() => addPreset(v)}
                      >
                        {added ? <CheckIcon /> : add.isPending ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
                      </Button>
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
