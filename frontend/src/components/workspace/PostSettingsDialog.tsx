"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ImageUpIcon, Loader2Icon, XIcon } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { VisibilityToggle } from "@/components/shared/Visibility";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  getPostPalettes,
  previewPostCard,
  uploadPostLogo,
  type CardTemplate,
  type HeadlineAlign,
  type HeadlineSize,
  type PaletteCatalog,
  type PhotoStyle,
  type PostSettings,
  type Visibility,
} from "@/lib/api";
import { queryKeys, useUpdateWorkspace, useWorkspacePostSettings } from "@/lib/queries";

function Segmented<T extends string>({
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

function ColorField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-1">
      <Label>{label}</Label>
      <div className="flex items-center gap-2">
        <input
          type="color"
          value={/^#[0-9a-fA-F]{6}$/.test(value) ? value : "#000000"}
          onChange={(e) => onChange(e.target.value)}
          aria-label={`${label} color`}
          className="size-8 shrink-0 cursor-pointer rounded border bg-transparent p-0"
        />
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="font-mono"
          placeholder="#000000"
        />
      </div>
    </div>
  );
}

const SELECT_CLS =
  "rounded-md border bg-background px-1.5 py-1 text-xs";

function TopicPaletteEditor({
  catalog,
  value,
  onChange,
}: {
  catalog: PaletteCatalog;
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
}) {
  const [newTopic, setNewTopic] = useState("");
  const names = Object.keys(catalog.palettes);
  const entries = Object.entries(value);
  const unused = catalog.topics.filter((t) => !(t in value));

  const setPalette = (topic: string, pal: string) =>
    onChange({ ...value, [topic]: pal });
  const remove = (topic: string) => {
    const next = { ...value };
    delete next[topic];
    onChange(next);
  };

  return (
    <div className="space-y-1.5">
      <Label className="text-xs text-muted-foreground">
        Topic overrides — unset topics use a sensible default
      </Label>
      {entries.map(([topic, pal]) => (
        <div key={topic} className="flex items-center gap-2">
          <span className="flex-1 truncate text-xs">{topic}</span>
          <select
            value={pal}
            onChange={(e) => setPalette(topic, e.target.value)}
            className={SELECT_CLS}
          >
            {names.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`Remove ${topic} override`}
            onClick={() => remove(topic)}
          >
            <XIcon />
          </Button>
        </div>
      ))}
      {unused.length > 0 ? (
        <div className="flex items-center gap-2 pt-0.5">
          <select
            value={newTopic}
            onChange={(e) => setNewTopic(e.target.value)}
            className={`${SELECT_CLS} flex-1`}
          >
            <option value="">Add a topic override…</option>
            {unused.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <Button
            type="button"
            size="xs"
            variant="outline"
            disabled={!newTopic}
            onClick={() => {
              setPalette(
                newTopic,
                catalog.topic_defaults[newTopic] ?? names[0],
              );
              setNewTopic("");
            }}
          >
            Add
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function PostSettingsDialog({
  workspaceId,
  open,
  onOpenChange,
}: {
  workspaceId: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const eff = useWorkspacePostSettings(workspaceId);
  const palettes = useQuery({
    queryKey: ["post-palettes"],
    queryFn: getPostPalettes,
    staleTime: Infinity,
  });
  const update = useUpdateWorkspace(workspaceId);
  const queryClient = useQueryClient();
  const [form, setForm] = useState<Partial<PostSettings>>({});

  const set = <K extends keyof PostSettings>(k: K, v: PostSettings[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  // the full settings the card will render with (effective + unsaved edits)
  const merged = useMemo<PostSettings | null>(
    () => (eff.data ? { ...eff.data, ...form } : null),
    [eff.data, form],
  );

  // --- live preview: debounced render of `merged` ---------------------------
  const urlRef = useRef<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const mergedKey = merged ? JSON.stringify(merged) : "";

  useEffect(() => {
    if (!open || !mergedKey) return;
    let cancelled = false;
    const t = setTimeout(async () => {
      setPreviewLoading(true);
      try {
        const url = await previewPostCard(JSON.parse(mergedKey) as PostSettings);
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        if (urlRef.current) URL.revokeObjectURL(urlRef.current);
        urlRef.current = url;
        setPreviewUrl(url);
      } catch {
        /* preview is best-effort */
      } finally {
        if (!cancelled) setPreviewLoading(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [open, mergedKey]);

  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  const fileRef = useRef<HTMLInputElement>(null);
  const onLogoFile = async (f: File) => {
    try {
      const { logo_sha } = await uploadPostLogo(f);
      set("logo_sha", logo_sha);
      toast.success("Logo uploaded");
    } catch (e) {
      toast.error("Could not upload logo", {
        description: e instanceof Error ? e.message : undefined,
      });
    }
  };

  const onSaved = () => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.workspacePostSettings(workspaceId),
    });
    setForm({});
    onOpenChange(false);
  };

  const save = () => {
    if (!merged) return;
    update.mutate(
      { post_settings: { ...merged } },
      {
        onSuccess: () => {
          toast.success("Post style saved for this workspace");
          onSaved();
        },
        onError: (e) =>
          toast.error("Could not save", { description: e.message }),
      },
    );
  };

  const resetToGlobal = () =>
    update.mutate(
      { post_settings: {} },
      {
        onSuccess: () => {
          toast.success("Reverted to the global default");
          onSaved();
        },
        onError: (e) =>
          toast.error("Could not reset", { description: e.message }),
      },
    );

  const logoSha = merged?.logo_sha;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Post style</DialogTitle>
          <DialogDescription>
            How this workspace generates and styles Instagram posts. Changes
            preview live; unset fields fall back to the global default.
          </DialogDescription>
        </DialogHeader>

        {eff.isPending || !merged ? (
          <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" />
            Loading…
          </div>
        ) : (
          <div className="grid gap-6 md:grid-cols-[1fr_320px]">
            {/* controls */}
            <div className="max-h-[62vh] space-y-4 overflow-y-auto pr-1">
              <div className="space-y-1">
                <Label>Template</Label>
                <Segmented<CardTemplate>
                  value={merged.card_template}
                  onChange={(v) => set("card_template", v)}
                  options={[
                    { value: "classic", label: "Classic" },
                    { value: "bold", label: "Bold" },
                    { value: "minimal", label: "Minimal" },
                  ]}
                />
              </div>

              <div className="space-y-1">
                <Label>Photo style</Label>
                <p className="text-xs text-muted-foreground">
                  Poster: full-bleed photo with a bold accent caption at the
                  bottom. Fitted keeps the whole photo on the card colour;
                  full-bleed crops it edge-to-edge behind the text.
                </p>
                <Segmented<PhotoStyle>
                  value={merged.card_photo_style ?? "poster"}
                  onChange={(v) => set("card_photo_style", v)}
                  options={[
                    { value: "poster", label: "Poster" },
                    { value: "fitted", label: "Fitted" },
                    { value: "cover", label: "Full-bleed" },
                  ]}
                />
              </div>

              <div className="space-y-2 rounded-lg border bg-muted/30 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <Label>Auto-theme by topic</Label>
                    <p className="text-xs text-muted-foreground">
                      Let each post pick a palette from its topic. The colors
                      below become the fallback.
                    </p>
                  </div>
                  <Switch
                    checked={merged.auto_theme}
                    onCheckedChange={(v) => set("auto_theme", v)}
                  />
                </div>
                {merged.auto_theme && palettes.data ? (
                  <div className="space-y-3 pt-1">
                    <div className="flex flex-wrap gap-1.5">
                      {Object.entries(palettes.data.palettes).map(
                        ([name, p]) => (
                          <span
                            key={name}
                            title={name}
                            className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]"
                            style={{
                              backgroundColor: p.card_bg,
                              color: p.card_text,
                            }}
                          >
                            <span
                              className="size-2 rounded-full"
                              style={{ backgroundColor: p.card_accent }}
                            />
                            {name}
                          </span>
                        ),
                      )}
                    </div>
                    <TopicPaletteEditor
                      catalog={palettes.data}
                      value={merged.topic_palettes}
                      onChange={(v) => set("topic_palettes", v)}
                    />
                  </div>
                ) : null}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <ColorField
                  label="Background"
                  value={merged.card_bg}
                  onChange={(v) => set("card_bg", v)}
                />
                <ColorField
                  label="Text"
                  value={merged.card_text}
                  onChange={(v) => set("card_text", v)}
                />
                <ColorField
                  label="Secondary"
                  value={merged.card_muted}
                  onChange={(v) => set("card_muted", v)}
                />
                <ColorField
                  label="Accent"
                  value={merged.card_accent}
                  onChange={(v) => set("card_accent", v)}
                />
              </div>

              <div className="flex flex-wrap gap-6">
                <div className="space-y-1">
                  <Label>Headline size</Label>
                  <Segmented<HeadlineSize>
                    value={merged.headline_size}
                    onChange={(v) => set("headline_size", v)}
                    options={[
                      { value: "s", label: "S" },
                      { value: "m", label: "M" },
                      { value: "l", label: "L" },
                    ]}
                  />
                </div>
                <div className="space-y-1">
                  <Label>Alignment</Label>
                  <Segmented<HeadlineAlign>
                    value={merged.headline_align}
                    onChange={(v) => set("headline_align", v)}
                    options={[
                      { value: "left", label: "Left" },
                      { value: "center", label: "Center" },
                    ]}
                  />
                </div>
              </div>

              <div className="space-y-1">
                <Label>Brand logo</Label>
                <div className="flex items-center gap-3">
                  {logoSha ? (
                    <span className="grid size-12 place-items-center rounded border bg-muted p-1">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={`/api/social/logo/${logoSha}.png`}
                        alt="logo"
                        className="max-h-full max-w-full"
                      />
                    </span>
                  ) : null}
                  <input
                    ref={fileRef}
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void onLogoFile(f);
                      e.target.value = "";
                    }}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => fileRef.current?.click()}
                  >
                    <ImageUpIcon data-icon="inline-start" />
                    {logoSha ? "Replace" : "Upload"}
                  </Button>
                  {logoSha ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => set("logo_sha", null)}
                    >
                      <XIcon data-icon="inline-start" />
                      Remove
                    </Button>
                  ) : null}
                </div>
              </div>

              <div className="space-y-1">
                <Label>Sign-off</Label>
                <Input
                  value={merged.sign_off}
                  onChange={(e) => set("sign_off", e.target.value)}
                  placeholder="via connect"
                />
              </div>

              <hr className="border-border" />

              <div className="space-y-1">
                <Label>Tone</Label>
                <Input
                  value={merged.tone}
                  onChange={(e) => set("tone", e.target.value)}
                  placeholder="neutral, factual, engaging"
                />
              </div>
              <div className="flex gap-3">
                <div className="flex-1 space-y-1">
                  <Label>Hashtags</Label>
                  <Input
                    type="number"
                    value={String(merged.hashtag_count)}
                    onChange={(e) =>
                      set("hashtag_count", Number(e.target.value) || 0)
                    }
                  />
                </div>
                <div className="flex-1 space-y-1">
                  <Label>Caption max</Label>
                  <Input
                    type="number"
                    value={String(merged.caption_max_chars)}
                    onChange={(e) =>
                      set("caption_max_chars", Number(e.target.value) || 0)
                    }
                  />
                </div>
              </div>
              <div className="space-y-1">
                <Label>Brand handle</Label>
                <Input
                  value={merged.brand_handle}
                  onChange={(e) => set("brand_handle", e.target.value)}
                  placeholder="@yourbrand (optional)"
                />
              </div>
              <VisibilityToggle
                value={merged.default_visibility as Visibility}
                onChange={(v) => set("default_visibility", v)}
                kind="post"
              />
            </div>

            {/* live preview */}
            <div className="space-y-2">
              <Label>Live preview</Label>
              <div className="relative aspect-square w-full overflow-hidden rounded-lg border bg-muted">
                {previewUrl ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={previewUrl}
                    alt="Post card preview"
                    className="size-full object-cover"
                  />
                ) : null}
                {previewLoading ? (
                  <div className="absolute inset-0 grid place-items-center bg-background/30">
                    <Loader2Icon className="size-6 animate-spin text-muted-foreground" />
                  </div>
                ) : null}
              </div>
              <p className="text-xs text-muted-foreground">
                A sample card — your generated posts use these styles.
              </p>
            </div>
          </div>
        )}

        <DialogFooter className="gap-2 sm:justify-between">
          <Button
            variant="ghost"
            size="sm"
            onClick={resetToGlobal}
            disabled={update.isPending}
          >
            Use global default
          </Button>
          <Button onClick={save} disabled={!merged || update.isPending}>
            {update.isPending ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : null}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
