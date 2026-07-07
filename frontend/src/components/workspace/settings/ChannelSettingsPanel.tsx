"use client";

import { useState } from "react";
import { Loader2Icon, LibraryBigIcon, SaveIcon } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import { CharacterCard } from "@/components/characters/CharacterCard";
import { CharacterPicker } from "@/components/characters/CharacterPicker";
import { PostSettingsDialog } from "@/components/workspace/PostSettingsDialog";
import { ScriptPresetsSection } from "@/components/workspace/settings/ScriptPresetsSection";
import { TopicChooser } from "@/components/workspace/TopicChooser";
import { WorkspaceSourcesDialog } from "@/components/workspace/WorkspaceSourcesDialog";
import { VisibilityToggle } from "@/components/shared/Visibility";
import { VoiceAuditionButton } from "@/components/voice/VoiceAuditionButton";
import { VoiceLibraryDialog } from "@/components/voice/VoiceLibraryDialog";
import { VoicePicker } from "@/components/voice/VoicePicker";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import type {
  ChannelPlatform,
  ScriptPreset,
  Visibility,
  Workspace,
  WorkspaceChannelUpdate,
} from "@/lib/api";
import {
  useCharacters,
  useDeleteWorkspace,
  useScriptPresets,
  useUpdateWorkspace,
  useUpdateWorkspaceChannel,
  useWorkspaceChannel,
} from "@/lib/queries";
import { useRouter } from "next/navigation";

const selectCls = "h-8 rounded-md border bg-background px-2 text-sm";
const PLATFORMS: { value: ChannelPlatform; label: string }[] = [
  { value: "youtube", label: "YouTube" },
  { value: "instagram", label: "Instagram" },
  { value: "x", label: "X" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "other", label: "Other" },
];

function ScriptTypeSelect({
  value,
  onChange,
  presets,
  nullLabel,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
  presets: ScriptPreset[];
  nullLabel: string;
}) {
  return (
    <select
      className={selectCls}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">{nullLabel}</option>
      {presets.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
          {p.builtin ? "" : " (custom)"}
        </option>
      ))}
    </select>
  );
}

/** The channel console's Settings tab: the publishing account, default voice /
 * character / script-type, script-type presets, the workspace focus, post
 * style, and the danger zone. */
export function ChannelSettingsPanel({
  workspaceId,
  workspace,
}: {
  workspaceId: number;
  workspace: Workspace;
}) {
  const channel = useWorkspaceChannel(workspaceId);
  const save = useUpdateWorkspaceChannel(workspaceId);
  const updateWs = useUpdateWorkspace(workspaceId);
  const del = useDeleteWorkspace();
  const characters = useCharacters();
  const presets = useScriptPresets();
  const router = useRouter();

  // ``draft`` is an overlay of edited fields; unedited fields read straight
  // from the fetched channel (avoids a setState-in-effect seed).
  const [draft, setDraft] = useState<WorkspaceChannelUpdate>({});
  const [postStyleOpen, setPostStyleOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [topics, setTopics] = useState<string[]>(workspace.topics);

  const set = (patch: Partial<WorkspaceChannelUpdate>) => setDraft((d) => ({ ...d, ...patch }));
  const cur = <K extends keyof WorkspaceChannelUpdate>(key: K): WorkspaceChannelUpdate[K] =>
    key in draft ? draft[key] : (channel.data?.[key] as WorkspaceChannelUpdate[K]);
  const saveChannel = () =>
    save.mutate(draft, {
      onSuccess: () => {
        setDraft({});
        toast.success("Channel saved");
      },
      onError: (e) => toast.error("Could not save", { description: e.message }),
    });

  const boundCharacter = (characters.data ?? []).find((c) => c.id === cur("default_character_id"));

  if (channel.isPending) return <Skeleton className="h-96 w-full" />;

  return (
    <div className="space-y-4">
      {/* account + voice + character + script default — one channel PUT */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            Channel
            <Button size="sm" disabled={save.isPending} onClick={saveChannel}>
              {save.isPending ? (
                <Loader2Icon className="animate-spin" data-icon="inline-start" />
              ) : (
                <SaveIcon data-icon="inline-start" />
              )}
              Save
            </Button>
          </CardTitle>
          <CardDescription>
            The account this workspace publishes as, and the voice, character and script type its
            content defaults to.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* account */}
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label>Platform</Label>
              <select
                className={selectCls}
                value={cur("platform") ?? "youtube"}
                onChange={(e) => set({ platform: e.target.value as ChannelPlatform })}
              >
                {PLATFORMS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label>Channel name</Label>
              <Input
                value={cur("channel_name") ?? ""}
                onChange={(e) => set({ channel_name: e.target.value })}
                placeholder="India Shorts"
              />
            </div>
            <div className="space-y-1">
              <Label>Handle</Label>
              <Input
                value={cur("channel_handle") ?? ""}
                onChange={(e) => set({ channel_handle: e.target.value })}
                placeholder="@indiashorts"
              />
            </div>
            <div className="space-y-1">
              <Label>Channel ID (optional)</Label>
              <Input
                value={cur("external_id") ?? ""}
                onChange={(e) => set({ external_id: e.target.value })}
                placeholder="UCxxxxxxxx"
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label>Zapier webhook URL</Label>
            <p className="text-xs text-muted-foreground">
              “Send to Zapier” routes this channel’s posts here (falling back to the global webhook).
            </p>
            <Input
              value={cur("zapier_webhook_url") ?? ""}
              onChange={(e) => set({ zapier_webhook_url: e.target.value })}
              placeholder="https://hooks.zapier.com/hooks/catch/…"
            />
          </div>

          {/* voice */}
          <div className="space-y-1">
            <Label>Default voice</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              <VoicePicker
                value={cur("default_voice_id") ?? null}
                onChange={(v) => set({ default_voice_id: v })}
                defaultLabel="Server default"
              />
              <VoiceAuditionButton voiceId={cur("default_voice_id") ?? undefined} />
              <Button variant="outline" size="sm" onClick={() => setLibraryOpen(true)}>
                <LibraryBigIcon data-icon="inline-start" />
                Voice library
              </Button>
            </div>
          </div>

          {/* character */}
          <div className="space-y-1">
            <Label>Default character</Label>
            <div className="flex items-center gap-1.5">
              <CharacterPicker
                value={cur("default_character_id") ?? null}
                onChange={(v) => set({ default_character_id: v })}
              />
              <Button variant="ghost" size="sm" render={<Link href="/characters" />}>
                Manage roster →
              </Button>
            </div>
            {boundCharacter ? <CharacterCard character={boundCharacter} /> : null}
          </div>

          {/* script default */}
          <div className="space-y-1">
            <Label>Default script type</Label>
            <ScriptTypeSelect
              value={cur("default_script_type") ?? null}
              onChange={(v) => set({ default_script_type: v })}
              presets={presets.data ?? []}
              nullLabel="Server default"
            />
          </div>
        </CardContent>
      </Card>

      <ScriptPresetsSection />

      {/* focus + post style */}
      <Card>
        <CardHeader>
          <CardTitle>Focus & style</CardTitle>
          <CardDescription>What this workspace tracks and how its cards look.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <Label>Topics</Label>
            <TopicChooser value={topics} onChange={setTopics} />
            <div className="flex items-center gap-2 pt-1">
              <Button
                size="sm"
                disabled={updateWs.isPending}
                onClick={() =>
                  updateWs.mutate(
                    { topics },
                    {
                      onSuccess: () => toast.success("Topics updated"),
                      onError: (e) => toast.error("Could not update", { description: e.message }),
                    },
                  )
                }
              >
                Save topics
              </Button>
              <Button size="sm" variant="outline" onClick={() => setSourcesOpen(true)}>
                Manage sources
              </Button>
              {workspace.query_fts ? <Badge variant="outline">{workspace.query_fts}</Badge> : null}
            </div>
          </div>

          <VisibilityToggle
            value={workspace.visibility}
            kind="workspace"
            onChange={(v: Visibility) =>
              updateWs.mutate(
                { visibility: v },
                { onError: (e) => toast.error("Could not update", { description: e.message }) },
              )
            }
          />

          <div className="flex items-center justify-between rounded-lg border px-3 py-2.5">
            <div>
              <p className="text-sm font-medium">Post style</p>
              <p className="text-xs text-muted-foreground">Card colours, template, photo style, logo.</p>
            </div>
            <Button variant="outline" size="sm" onClick={() => setPostStyleOpen(true)}>
              Edit post style
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* danger zone */}
      <Card>
        <CardHeader>
          <CardTitle>Danger zone</CardTitle>
        </CardHeader>
        <CardContent>
          <Button
            variant="destructive"
            size="sm"
            disabled={del.isPending}
            onClick={() =>
              del.mutate(workspaceId, {
                onSuccess: () => {
                  toast.success("Workspace deleted");
                  router.push("/workspaces");
                },
                onError: (e) => toast.error("Could not delete", { description: e.message }),
              })
            }
          >
            Delete workspace
          </Button>
        </CardContent>
      </Card>

      <PostSettingsDialog workspaceId={workspaceId} open={postStyleOpen} onOpenChange={setPostStyleOpen} />
      <WorkspaceSourcesDialog workspaceId={workspaceId} open={sourcesOpen} onOpenChange={setSourcesOpen} />
      <VoiceLibraryDialog open={libraryOpen} onOpenChange={setLibraryOpen} />
    </div>
  );
}
