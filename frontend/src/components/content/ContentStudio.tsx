"use client";

import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FilmIcon, ImageIcon, Loader2Icon, PlusIcon, Trash2Icon, Wand2Icon } from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  approveContent,
  cancelCampaign,
  cardUrl,
  CAPTION_STYLES,
  CONTENT_FORMATS,
  type CampaignDetail,
  type CaptionStyle,
  type ContentFormat,
  type ContentItem,
  type ContentOptions,
  type MemeContent,
  type StorySource,
  type VoiceOption,
  createCampaign,
  deleteCampaign,
  editContent,
  getCampaign,
  getContentTrust,
  getReelCapabilities,
  getVoices,
  listCampaigns,
  publishContent,
  reelUrl,
  rejectContent,
  rerenderContent,
  scheduleContent,
} from "@/lib/api";
import { absoluteTime, relativeTime } from "@/lib/format";
import { TrustPanel } from "@/components/trust/TrustPanel";

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  draft: "outline",
  verifying: "outline",
  approved: "secondary",
  scheduled: "secondary",
  published: "default",
  rejected: "outline",
  failed: "destructive",
  flagged: "destructive",
  corrected: "destructive",
  retracted: "destructive",
};

/** Planner significance label -> badge tone, so "breaking 5/5" and
 * "routine 1/5" stop looking alike (labels are a closed set: the backend
 * always derives them from the score). */
const SIGNIFICANCE_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  breaking: "destructive",
  major: "default",
  notable: "secondary",
  minor: "outline",
  routine: "outline",
};

/** Formats whose media (image/video) must be re-rendered after an edit. */
const MEDIA_FORMATS = new Set<ContentFormat>(["ig_card", "ig_carousel", "ig_reel", "meme"]);

const selectCls = "h-8 rounded-md border bg-background px-2 text-sm";

// --- small text<->list helpers ----------------------------------------------
const tagsToText = (h?: string[]) => (h ?? []).join(" ");
const textToTags = (t: string) =>
  t
    .split(/[\s,]+/)
    .map((s) => s.replace(/^#/, "").trim())
    .filter(Boolean);
const linesToText = (a?: string[]) => (a ?? []).join("\n");
const textToLines = (t: string) =>
  t
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);

function useVoices() {
  return useQuery({ queryKey: ["voices"], queryFn: getVoices, staleTime: Infinity });
}

function useReelCapabilities(): { presenter: boolean; video: boolean; zapier: boolean } {
  const { data } = useQuery({
    queryKey: ["reel-capabilities"],
    queryFn: getReelCapabilities,
    staleTime: Infinity,
  });
  return {
    presenter: data?.presenter ?? false,
    video: data?.video ?? false,
    zapier: data?.zapier ?? false,
  };
}

/** The "AI presenter" (HeyGen avatar) toggle — shown only when the server has
 * HeyGen configured (otherwise the backend silently falls back to a slideshow,
 * so the control would be a no-op). */
function PresenterToggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  if (!useReelCapabilities().presenter) return null;
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-muted-foreground">AI presenter</span>
      <Switch checked={checked} onCheckedChange={onChange} />
    </label>
  );
}

/** Stock-video b-roll toggle — shown only when a Pexels key is configured. */
function VideoToggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  if (!useReelCapabilities().video) return null;
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-muted-foreground">Video b-roll</span>
      <Switch checked={checked} onCheckedChange={onChange} />
    </label>
  );
}

function CaptionStylePicker({ value, onChange }: { value: CaptionStyle; onChange: (v: CaptionStyle) => void }) {
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-muted-foreground">Captions</span>
      <select className={selectCls} value={value} onChange={(e) => onChange(e.target.value as CaptionStyle)}>
        {CAPTION_STYLES.map((c) => (
          <option key={c.value} value={c.value}>
            {c.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function VoicePicker({ value, onChange }: { value: string | null; onChange: (v: string | null) => void }) {
  const voices = useVoices();
  return (
    <select className={selectCls} value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
      <option value="">Default voice</option>
      {(voices.data ?? []).map((v: VoiceOption) => (
        <option key={v.id} value={v.id}>
          {v.name}
          {v.description ? ` — ${v.description}` : ""}
        </option>
      ))}
    </select>
  );
}

/** Reel render controls shared by the create form and the reel editor. */
interface ReelControlsState {
  voiceId: string | null;
  music: boolean;
  captionStyle: CaptionStyle;
  video: boolean;
  presenter: boolean;
}
function ReelControls({
  state,
  set,
  children,
}: {
  state: ReelControlsState;
  set: (patch: Partial<ReelControlsState>) => void;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
      <label className="flex items-center gap-1.5">
        <span className="text-muted-foreground">Voice</span>
        <VoicePicker value={state.voiceId} onChange={(v) => set({ voiceId: v })} />
      </label>
      <label className="flex items-center gap-1.5">
        <span className="text-muted-foreground">Music</span>
        <Switch checked={state.music} onCheckedChange={(v) => set({ music: v })} />
      </label>
      <CaptionStylePicker value={state.captionStyle} onChange={(v) => set({ captionStyle: v })} />
      <VideoToggle checked={state.video} onChange={(v) => set({ video: v })} />
      <PresenterToggle checked={state.presenter} onChange={(v) => set({ presenter: v })} />
      {children}
    </div>
  );
}
const reelOptionsFrom = (s: ReelControlsState): ContentOptions => ({
  music: s.music,
  caption_style: s.captionStyle,
  video: s.video,
  presenter: s.presenter,
  ...(s.voiceId ? { voice_id: s.voiceId } : {}),
});

// === create campaign =========================================================

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
}) {
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-muted-foreground">{label}</span>
      <Input
        type="number"
        className="h-8 w-16"
        value={value}
        min={min}
        max={max}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

/** The campaign create form. With ``workspaceId`` it seeds from the workspace
 * (no free-text topic); otherwise it is topic-driven. */
function CreateCampaign({ workspaceId }: { workspaceId?: number }) {
  const qc = useQueryClient();
  const [topic, setTopic] = useState("");
  // auto: send formats [] and let the editorial planner pick the mix + media
  const [auto, setAuto] = useState(false);
  const [formats, setFormats] = useState<Set<ContentFormat>>(new Set(["ig_reel"]));
  const [tone, setTone] = useState("");
  const [style, setStyle] = useState("");
  const [hashtagCount, setHashtagCount] = useState(8);
  const [sceneCount, setSceneCount] = useState(3);
  const [slideCount, setSlideCount] = useState(5);
  const [threadLength, setThreadLength] = useState(5);
  const [reel, setReel] = useState<ReelControlsState>({
    voiceId: null,
    music: true,
    captionStyle: "karaoke",
    video: true,
    presenter: false,
  });

  const create = useMutation({
    mutationFn: () => {
      const options: ContentOptions = {
        hashtag_count: hashtagCount,
        scene_count: sceneCount,
        slide_count: slideCount,
        thread_length: threadLength,
      };
      if (tone.trim()) options.tone = tone.trim();
      if (style.trim()) options.style = style.trim();
      // auto mode leaves the reel controls at server defaults so the planner's
      // media decision (photo slideshow vs video b-roll) is not overridden
      if (!auto && formats.has("ig_reel")) Object.assign(options, reelOptionsFrom(reel));
      const seed = workspaceId ? { workspace_id: workspaceId } : { topic: topic.trim() };
      return createCampaign({ ...seed, formats: auto ? [] : [...formats], options });
    },
    onSuccess: () => {
      toast.success("Campaign started", { description: "Drafts appear below once generation finishes." });
      setTopic("");
      void qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
    onError: (e: Error) => toast.error("Could not start campaign", { description: e.message }),
  });

  const toggle = (f: ContentFormat) =>
    setFormats((prev) => {
      const next = new Set(prev);
      if (next.has(f)) next.delete(f);
      else next.add(f);
      return next;
    });

  const hasReel = !auto && formats.has("ig_reel");
  const hasCarousel = !auto && formats.has("ig_carousel");
  const hasThread = !auto && formats.has("x_thread");
  const canSubmit = (workspaceId ? true : topic.trim().length > 0) && (auto || formats.size > 0);

  return (
    <div className="mb-6 space-y-3 rounded-xl border bg-muted/30 p-4">
      {workspaceId ? (
        <p className="text-sm text-muted-foreground">
          Generate posts grounded in this workspace&rsquo;s documents.
        </p>
      ) : (
        <Input
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="Topic to make content about (e.g. RBI repo rate decision)"
          maxLength={400}
        />
      )}
      <div className="flex flex-wrap gap-1.5">
        <Button
          type="button"
          size="xs"
          variant={auto ? "secondary" : "outline"}
          aria-pressed={auto}
          title="The editor decides: how big the story is, which formats to make, and the media treatment"
          onClick={() => setAuto((a) => !a)}
        >
          <Wand2Icon data-icon="inline-start" />
          Auto
        </Button>
        {CONTENT_FORMATS.map((f) => (
          <Button
            key={f.value}
            type="button"
            size="xs"
            className={auto ? "opacity-40" : undefined}
            variant={!auto && formats.has(f.value) ? "secondary" : "outline"}
            aria-pressed={!auto && formats.has(f.value)}
            onClick={() => {
              setAuto(false);
              toggle(f.value);
            }}
          >
            {f.label}
          </Button>
        ))}
      </div>
      {auto ? (
        <p className="text-xs text-muted-foreground">
          Auto: the planner judges the story&rsquo;s significance and picks the format mix (reel, card,
          carousel, thread, LinkedIn or meme) plus the media treatment for each.
        </p>
      ) : null}

      {/* shared generation controls */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
        <label className="flex items-center gap-1.5">
          <span className="text-muted-foreground">Tone</span>
          <Input
            className="h-8 w-48"
            placeholder="neutral, factual, engaging"
            value={tone}
            onChange={(e) => setTone(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-1.5">
          <span className="text-muted-foreground">Style note</span>
          <Input
            className="h-8 w-48"
            placeholder="optional (e.g. explain like to a beginner)"
            value={style}
            maxLength={200}
            onChange={(e) => setStyle(e.target.value)}
          />
        </label>
        <NumberField label="Hashtags" value={hashtagCount} min={0} max={30} onChange={setHashtagCount} />
        {hasCarousel ? (
          <NumberField label="Slides" value={slideCount} min={3} max={8} onChange={setSlideCount} />
        ) : null}
        {hasThread ? (
          <NumberField label="Thread length" value={threadLength} min={2} max={10} onChange={setThreadLength} />
        ) : null}
        {hasReel ? (
          <label className="flex items-center gap-1.5">
            <span className="text-muted-foreground">Scenes</span>
            <select className={selectCls} value={sceneCount} onChange={(e) => setSceneCount(Number(e.target.value))}>
              {[3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      {hasReel ? <ReelControls state={reel} set={(p) => setReel((s) => ({ ...s, ...p }))} /> : null}

      <Button disabled={!canSubmit || create.isPending} onClick={() => create.mutate()}>
        {create.isPending ? <Loader2Icon className="animate-spin" data-icon="inline-start" /> : null}
        Generate content
      </Button>
    </div>
  );
}

// === per-format editors ======================================================

/** Save bar shared by every editor. ``media`` formats persist + re-render (the
 * preview updates after the render job); text formats persist synchronously. */
function useItemSave(item: ContentItem, onRegenerating: () => void) {
  const qc = useQueryClient();
  const media = MEDIA_FORMATS.has(item.format);
  return useMutation<unknown, Error, { content: Record<string, unknown>; options?: ContentOptions }>({
    mutationFn: (args) =>
      media
        ? rerenderContent(item.id, { content: args.content, options: args.options ?? {} })
        : editContent(item.id, args.content),
    onSuccess: () => {
      if (media) {
        toast.success("Saved — re-rendering", { description: "Preview updates in ~30s." });
        onRegenerating();
      } else {
        toast.success("Saved");
      }
      void qc.invalidateQueries({ queryKey: ["campaign", item.campaign_id] });
    },
    onError: (e: Error) => toast.error("Could not save", { description: e.message }),
  });
}

function SaveButton({ media, pending, onClick }: { media: boolean; pending: boolean; onClick: () => void }) {
  return (
    <Button size="sm" disabled={pending} onClick={onClick}>
      {pending ? <Loader2Icon className="animate-spin" data-icon="inline-start" /> : <Wand2Icon data-icon="inline-start" />}
      {media ? "Save & re-render" : "Save edits"}
    </Button>
  );
}

const editorBox = "mt-2 space-y-3 rounded-lg border bg-background p-3";
const fieldLabel = "text-xs font-medium text-muted-foreground";

function Labeled({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <p className={fieldLabel}>{label}</p>
      {children}
    </div>
  );
}

function HashtagsField({ value, onChange }: { value?: string[]; onChange: (h: string[]) => void }) {
  return (
    <Labeled label="Hashtags (space-separated, no #)">
      <Input value={tagsToText(value)} onChange={(e) => onChange(textToTags(e.target.value))} />
    </Labeled>
  );
}

interface CardShape {
  headline: string;
  caption: string;
  key_points?: string[];
  hashtags?: string[];
  source_label?: string;
  alt_text?: string;
  image_query?: string;
}
function CardEditor({ item, onRegenerating }: { item: ContentItem; onRegenerating: () => void }) {
  const [c, setC] = useState<CardShape>(() => structuredClone(item.content) as unknown as CardShape);
  const save = useItemSave(item, onRegenerating);
  return (
    <div className={editorBox}>
      <Labeled label="Headline">
        <Input value={c.headline} onChange={(e) => setC((p) => ({ ...p, headline: e.target.value }))} />
      </Labeled>
      <Labeled label="Caption">
        <Textarea
          className="min-h-16 text-sm"
          value={c.caption}
          onChange={(e) => setC((p) => ({ ...p, caption: e.target.value }))}
        />
      </Labeled>
      <Labeled label="Key points (one per line)">
        <Textarea
          className="min-h-16 text-sm"
          value={linesToText(c.key_points)}
          onChange={(e) => setC((p) => ({ ...p, key_points: textToLines(e.target.value) }))}
        />
      </Labeled>
      <Labeled label="Background photo (2-4 words, optional)">
        <Input value={c.image_query ?? ""} onChange={(e) => setC((p) => ({ ...p, image_query: e.target.value }))} />
      </Labeled>
      <HashtagsField value={c.hashtags} onChange={(h) => setC((p) => ({ ...p, hashtags: h }))} />
      <SaveButton media pending={save.isPending} onClick={() => save.mutate({ content: c as unknown as Record<string, unknown> })} />
    </div>
  );
}

interface CarouselSlideShape {
  heading: string;
  bullets?: string[];
  image_query?: string;
}
interface CarouselShape {
  title: string;
  slides: CarouselSlideShape[];
  caption: string;
  hashtags?: string[];
  source_label?: string;
  alt_text?: string;
  image_query?: string;
}
function CarouselEditor({ item, onRegenerating }: { item: ContentItem; onRegenerating: () => void }) {
  const [c, setC] = useState<CarouselShape>(() => structuredClone(item.content) as unknown as CarouselShape);
  const save = useItemSave(item, onRegenerating);
  const setSlide = (i: number, patch: Partial<CarouselSlideShape>) =>
    setC((p) => ({ ...p, slides: p.slides.map((s, j) => (j === i ? { ...s, ...patch } : s)) }));
  return (
    <div className={editorBox}>
      <Labeled label="Cover title">
        <Input value={c.title} onChange={(e) => setC((p) => ({ ...p, title: e.target.value }))} />
      </Labeled>
      <Labeled label="Cover background photo (2-4 words, optional)">
        <Input value={c.image_query ?? ""} onChange={(e) => setC((p) => ({ ...p, image_query: e.target.value }))} />
      </Labeled>
      {c.slides.map((s, i) => (
        <div key={i} className="space-y-1 rounded-md border p-2">
          <div className="flex items-center justify-between">
            <p className={fieldLabel}>Slide {i + 1}</p>
            <Button
              size="xs"
              variant="ghost"
              onClick={() => setC((p) => ({ ...p, slides: p.slides.filter((_, j) => j !== i) }))}
            >
              <Trash2Icon data-icon="inline-start" />
              Remove
            </Button>
          </div>
          <Input
            placeholder="Slide heading"
            value={s.heading}
            onChange={(e) => setSlide(i, { heading: e.target.value })}
          />
          <Textarea
            className="min-h-12 text-sm"
            placeholder="Bullets (one per line)"
            value={linesToText(s.bullets)}
            onChange={(e) => setSlide(i, { bullets: textToLines(e.target.value) })}
          />
          <Input
            className="text-xs"
            placeholder="Background photo (2-4 words, optional)"
            value={s.image_query ?? ""}
            onChange={(e) => setSlide(i, { image_query: e.target.value })}
          />
        </div>
      ))}
      <Button
        size="xs"
        variant="outline"
        onClick={() => setC((p) => ({ ...p, slides: [...p.slides, { heading: "", bullets: [] }] }))}
      >
        <PlusIcon data-icon="inline-start" />
        Add slide
      </Button>
      <Labeled label="Caption">
        <Textarea
          className="min-h-16 text-sm"
          value={c.caption}
          onChange={(e) => setC((p) => ({ ...p, caption: e.target.value }))}
        />
      </Labeled>
      <HashtagsField value={c.hashtags} onChange={(h) => setC((p) => ({ ...p, hashtags: h }))} />
      <SaveButton media pending={save.isPending} onClick={() => save.mutate({ content: c as unknown as Record<string, unknown> })} />
    </div>
  );
}

function MemeEditor({ item, onRegenerating }: { item: ContentItem; onRegenerating: () => void }) {
  const [c, setC] = useState<MemeContent>(() => structuredClone(item.content) as unknown as MemeContent);
  const save = useItemSave(item, onRegenerating);
  return (
    <div className={editorBox}>
      <Labeled label="Top text">
        <Input value={c.top_text} onChange={(e) => setC((p) => ({ ...p, top_text: e.target.value }))} />
      </Labeled>
      <Labeled label="Bottom text">
        <Input value={c.bottom_text} onChange={(e) => setC((p) => ({ ...p, bottom_text: e.target.value }))} />
      </Labeled>
      <Labeled label="Background photo (2-4 words)">
        <Input value={c.image_query} onChange={(e) => setC((p) => ({ ...p, image_query: e.target.value }))} />
      </Labeled>
      <Labeled label="Caption">
        <Textarea
          className="min-h-16 text-sm"
          value={c.caption}
          onChange={(e) => setC((p) => ({ ...p, caption: e.target.value }))}
        />
      </Labeled>
      <HashtagsField value={c.hashtags} onChange={(h) => setC((p) => ({ ...p, hashtags: h }))} />
      <SaveButton media pending={save.isPending} onClick={() => save.mutate({ content: c as unknown as Record<string, unknown> })} />
    </div>
  );
}

interface ThreadShape {
  tweets: string[];
  hashtags?: string[];
}
function ThreadEditor({ item, onRegenerating }: { item: ContentItem; onRegenerating: () => void }) {
  const [c, setC] = useState<ThreadShape>(() => structuredClone(item.content) as unknown as ThreadShape);
  const save = useItemSave(item, onRegenerating);
  const setTweet = (i: number, v: string) =>
    setC((p) => ({ ...p, tweets: p.tweets.map((t, j) => (j === i ? v : t)) }));
  return (
    <div className={editorBox}>
      {c.tweets.map((t, i) => (
        <div key={i} className="space-y-1">
          <div className="flex items-center justify-between">
            <p className={fieldLabel}>
              Tweet {i + 1} · {t.length}/270
            </p>
            <Button
              size="xs"
              variant="ghost"
              onClick={() => setC((p) => ({ ...p, tweets: p.tweets.filter((_, j) => j !== i) }))}
            >
              <Trash2Icon data-icon="inline-start" />
              Remove
            </Button>
          </div>
          <Textarea
            className="min-h-14 text-sm"
            value={t}
            maxLength={270}
            onChange={(e) => setTweet(i, e.target.value)}
          />
        </div>
      ))}
      <Button size="xs" variant="outline" onClick={() => setC((p) => ({ ...p, tweets: [...p.tweets, ""] }))}>
        <PlusIcon data-icon="inline-start" />
        Add tweet
      </Button>
      <HashtagsField value={c.hashtags} onChange={(h) => setC((p) => ({ ...p, hashtags: h }))} />
      <SaveButton media={false} pending={save.isPending} onClick={() => save.mutate({ content: c as unknown as Record<string, unknown> })} />
    </div>
  );
}

interface LinkedInShape {
  body: string;
  hashtags?: string[];
}
function LinkedInEditor({ item, onRegenerating }: { item: ContentItem; onRegenerating: () => void }) {
  const [c, setC] = useState<LinkedInShape>(() => structuredClone(item.content) as unknown as LinkedInShape);
  const save = useItemSave(item, onRegenerating);
  return (
    <div className={editorBox}>
      <Labeled label={`Post body · ${c.body.length}/2800`}>
        <Textarea
          className="min-h-40 text-sm"
          value={c.body}
          maxLength={2800}
          onChange={(e) => setC((p) => ({ ...p, body: e.target.value }))}
        />
      </Labeled>
      <HashtagsField value={c.hashtags} onChange={(h) => setC((p) => ({ ...p, hashtags: h }))} />
      <SaveButton media={false} pending={save.isPending} onClick={() => save.mutate({ content: c as unknown as Record<string, unknown> })} />
    </div>
  );
}

interface ReelScene {
  narration: string;
  on_screen_caption: string;
  image_query: string;
  bullets?: string[];
}
interface ReelShape {
  title: string;
  image_query?: string;
  scenes: ReelScene[];
  caption?: string;
  hashtags?: string[];
  source_label?: string;
  alt_text?: string;
}
function ReelEditor({ item, onRegenerating }: { item: ContentItem; onRegenerating: () => void }) {
  const [c, setC] = useState<ReelShape>(() => structuredClone(item.content) as unknown as ReelShape);
  const [reel, setReel] = useState<ReelControlsState>({
    voiceId: null,
    music: true,
    captionStyle: "karaoke",
    video: true,
    presenter: false,
  });
  const save = useItemSave(item, onRegenerating);
  const setScene = (i: number, patch: Partial<ReelScene>) =>
    setC((p) => ({ ...p, scenes: p.scenes.map((s, j) => (j === i ? { ...s, ...patch } : s)) }));

  return (
    <div className={editorBox}>
      <div className="space-y-1">
        <p className={fieldLabel}>Hook</p>
        <Input value={c.title} onChange={(e) => setC((p) => ({ ...p, title: e.target.value }))} />
        <Input
          className="text-xs"
          placeholder="Hook image search (e.g. Reserve Bank India building)"
          value={c.image_query ?? ""}
          onChange={(e) => setC((p) => ({ ...p, image_query: e.target.value }))}
        />
      </div>
      {c.scenes.map((s, i) => (
        <div key={i} className="space-y-1 rounded-md border p-2">
          <p className={fieldLabel}>Scene {i + 1}</p>
          <Textarea
            className="min-h-12 text-sm"
            placeholder="Narration (spoken)"
            value={s.narration}
            onChange={(e) => setScene(i, { narration: e.target.value })}
          />
          <Input
            className="text-sm"
            placeholder="On-screen caption"
            value={s.on_screen_caption}
            onChange={(e) => setScene(i, { on_screen_caption: e.target.value })}
          />
          <Input
            className="text-xs"
            placeholder="Image search query"
            value={s.image_query ?? ""}
            onChange={(e) => setScene(i, { image_query: e.target.value })}
          />
        </div>
      ))}
      <ReelControls state={reel} set={(p) => setReel((st) => ({ ...st, ...p }))}>
        <SaveButton
          media
          pending={save.isPending}
          onClick={() => save.mutate({ content: c as unknown as Record<string, unknown>, options: reelOptionsFrom(reel) })}
        />
      </ReelControls>
    </div>
  );
}

/** Dispatch to the right editor for the item's format. */
function ContentEditor({ item, onRegenerating }: { item: ContentItem; onRegenerating: () => void }) {
  switch (item.format) {
    case "ig_card":
      return <CardEditor item={item} onRegenerating={onRegenerating} />;
    case "ig_carousel":
      return <CarouselEditor item={item} onRegenerating={onRegenerating} />;
    case "meme":
      return <MemeEditor item={item} onRegenerating={onRegenerating} />;
    case "x_thread":
      return <ThreadEditor item={item} onRegenerating={onRegenerating} />;
    case "linkedin_post":
      return <LinkedInEditor item={item} onRegenerating={onRegenerating} />;
    case "ig_reel":
      return <ReelEditor item={item} onRegenerating={onRegenerating} />;
    default:
      return null;
  }
}

// === preview / sources / actions =============================================

function ItemPreview({ item }: { item: ContentItem }) {
  const c = item.content as Record<string, unknown>;
  if (item.format === "ig_reel") {
    return item.card_shas.length ? (
      <video
        key={item.card_shas[0]}
        src={reelUrl(item.card_shas[0])}
        controls
        playsInline
        className="max-h-96 rounded-lg border"
      />
    ) : (
      <p className="text-xs text-muted-foreground">No video rendered.</p>
    );
  }
  if (item.format === "ig_card" || item.format === "ig_carousel" || item.format === "meme") {
    return item.card_shas.length ? (
      <div className="flex flex-wrap gap-2">
        {item.card_shas.map((sha) => (
          // eslint-disable-next-line @next/next/no-img-element
          <img key={sha} src={cardUrl(sha)} alt="" className="h-48 rounded-lg border" />
        ))}
      </div>
    ) : (
      <p className="text-xs text-muted-foreground">No card rendered.</p>
    );
  }
  const text = Array.isArray(c.tweets)
    ? (c.tweets as string[]).join("\n\n")
    : typeof c.body === "string"
      ? c.body
      : JSON.stringify(c, null, 2);
  return <p className="whitespace-pre-wrap text-sm">{text}</p>;
}

/** Lazily fetches and renders the S6 trust report for one item. */
function TrustPanelForItem({ item }: { item: ContentItem }) {
  const { data } = useQuery({
    queryKey: ["content-trust", item.id, item.status, item.edited],
    queryFn: () => getContentTrust(item.id),
    staleTime: 30_000,
  });
  if (!data) return null;
  return <TrustPanel report={data} />;
}

function SourcesPanel({ item }: { item: ContentItem }) {
  const sources = item.sources ?? [];
  if (sources.length === 0) return null;
  const cited = item.grounding?.cited_count;
  return (
    <details className="rounded-md border bg-muted/20 px-3 py-2 text-sm">
      <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
        Sources ({sources.length})
        {typeof cited === "number" ? ` · ${cited} cited` : ""}
      </summary>
      <ul className="mt-2 space-y-1.5">
        {sources.map((s: StorySource, i: number) => (
          <li key={i} className="text-xs">
            <span className="text-muted-foreground">[{s.ref}]</span>{" "}
            {s.url ? (
              <a href={s.url} target="_blank" rel="noopener noreferrer" className="underline">
                {s.title || s.source_name || s.url}
              </a>
            ) : (
              <span>{s.title || s.source_name || "source"}</span>
            )}
            {s.source_name && (s.title || s.url) ? (
              <span className="text-muted-foreground"> — {s.source_name}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

/** When-it-publishes / when-it-published line. */
function ItemMeta({ item }: { item: ContentItem }) {
  if (item.status === "scheduled" && item.scheduled_at) {
    return <p className="text-xs text-muted-foreground">Scheduled for {absoluteTime(item.scheduled_at)}</p>;
  }
  if (item.status === "published" && item.published_at) {
    return <p className="text-xs text-muted-foreground">Published {absoluteTime(item.published_at)}</p>;
  }
  return null;
}

/** Local-time datetime input that emits a UTC ISO string on submit. */
function SchedulePicker({ onSet, onCancel, busy }: { onSet: (iso: string) => void; onCancel: () => void; busy: boolean }) {
  const [value, setValue] = useState("");
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <input type="datetime-local" className={selectCls} value={value} onChange={(e) => setValue(e.target.value)} />
      <Button
        size="xs"
        disabled={busy || !value}
        onClick={() => {
          const d = new Date(value); // datetime-local is local time → ISO is UTC
          if (Number.isNaN(d.getTime())) {
            toast.error("Pick a valid date and time");
            return;
          }
          onSet(d.toISOString());
        }}
      >
        Set time
      </Button>
      <Button size="xs" variant="ghost" disabled={busy} onClick={onCancel}>
        Cancel
      </Button>
    </div>
  );
}

function ItemActions({ item }: { item: ContentItem }) {
  const qc = useQueryClient();
  const [scheduling, setScheduling] = useState(false);
  const { zapier } = useReelCapabilities();
  const refresh = () => void qc.invalidateQueries({ queryKey: ["campaign", item.campaign_id] });
  const onErr = (e: Error) => toast.error("Action failed", { description: e.message });

  const approve = useMutation({ mutationFn: () => approveContent(item.id), onSuccess: refresh, onError: onErr });
  const reject = useMutation({ mutationFn: () => rejectContent(item.id), onSuccess: refresh, onError: onErr });
  const publish = useMutation({
    mutationFn: (target: "direct" | "zapier") => publishContent(item.id, target),
    onSuccess: (_d, target) => {
      toast.success(target === "zapier" ? "Sent to Zapier" : "Publish queued");
      refresh();
    },
    onError: onErr,
  });
  const schedule = useMutation({
    mutationFn: (whenIso: string) => scheduleContent(item.id, whenIso),
    onSuccess: () => {
      toast.success("Scheduled");
      setScheduling(false);
      refresh();
    },
    onError: onErr,
  });

  const busy = approve.isPending || reject.isPending || publish.isPending || schedule.isPending;

  if (item.status === "published") {
    return <p className="text-xs text-muted-foreground">Published — no further actions.</p>;
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button size="xs" variant="outline" disabled={busy} onClick={() => approve.mutate()}>
          Approve
        </Button>
        <Button size="xs" variant="outline" disabled={busy} onClick={() => reject.mutate()}>
          Reject
        </Button>
        <Button size="xs" variant="outline" disabled={busy} onClick={() => setScheduling((s) => !s)}>
          {item.status === "scheduled" ? "Reschedule" : "Schedule"}
        </Button>
        <Button size="xs" disabled={busy} onClick={() => publish.mutate("direct")}>
          Publish now
        </Button>
        {zapier ? (
          <Button size="xs" variant="outline" disabled={busy} onClick={() => publish.mutate("zapier")}>
            Send to Zapier
          </Button>
        ) : null}
      </div>
      {scheduling ? (
        <SchedulePicker busy={busy} onSet={(iso) => schedule.mutate(iso)} onCancel={() => setScheduling(false)} />
      ) : null}
    </div>
  );
}

// === campaign card ===========================================================

function CampaignActions({ d, onGone }: { d: CampaignDetail; onGone: () => void }) {
  const qc = useQueryClient();
  const running = ["pending", "running"].includes(d.status);
  const refresh = () => void qc.invalidateQueries({ queryKey: ["campaign", d.id] });

  const cancel = useMutation({
    mutationFn: () => cancelCampaign(d.id),
    onSuccess: () => {
      toast.success("Cancelling…");
      refresh();
    },
    onError: (e: Error) => toast.error("Could not cancel", { description: e.message }),
  });
  const retry = useMutation({
    mutationFn: () => createCampaign({ topic: d.subject, formats: d.formats as ContentFormat[] }),
    onSuccess: () => {
      toast.success("Retrying campaign", { description: "New drafts appear shortly." });
      void qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
    onError: (e: Error) => toast.error("Could not retry", { description: e.message }),
  });
  const remove = useMutation({
    mutationFn: () => deleteCampaign(d.id),
    onSuccess: () => {
      toast.success("Campaign deleted");
      onGone();
      void qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
    onError: (e: Error) => toast.error("Could not delete", { description: e.message }),
  });

  return (
    <div className="ml-auto flex items-center gap-1.5">
      {running ? (
        <Button size="xs" variant="outline" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
          Cancel
        </Button>
      ) : null}
      {d.status === "failed" && d.input_type === "topic" ? (
        <Button size="xs" variant="outline" disabled={retry.isPending} onClick={() => retry.mutate()}>
          {retry.isPending ? <Loader2Icon className="animate-spin" data-icon="inline-start" /> : null}
          Retry
        </Button>
      ) : null}
      <Button
        size="xs"
        variant="ghost"
        disabled={remove.isPending}
        onClick={() => {
          if (window.confirm(`Delete the campaign "${d.subject}" and all its drafts?`)) remove.mutate();
        }}
      >
        <Trash2Icon data-icon="inline-start" />
        Delete
      </Button>
    </div>
  );
}

function CampaignCard({ id }: { id: number }) {
  const [editing, setEditing] = useState<number | null>(null);
  const [regenUntil, setRegenUntil] = useState(0);
  const [gone, setGone] = useState(false);
  const campaign = useQuery<CampaignDetail>({
    queryKey: ["campaign", id],
    queryFn: () => getCampaign(id),
    enabled: !gone,
    refetchInterval: (q) =>
      ["pending", "running"].includes(q.state.data?.status ?? "") || Date.now() < regenUntil ? 2500 : false,
  });

  if (gone) return null;
  if (campaign.isPending) return <Skeleton className="h-24 w-full" />;
  if (campaign.isError) return <QueryError error={campaign.error} onRetry={() => void campaign.refetch()} />;

  const d = campaign.data;
  const running = ["pending", "running"].includes(d.status);
  // the backend persists plan+formats BEFORE generating, so an empty formats
  // list on a running campaign means the planner itself is still deciding
  const progress = !running
    ? null
    : d.formats.length === 0
      ? "Planning coverage…"
      : `Generating ${Math.min(d.items.length + 1, d.formats.length)}/${d.formats.length}: ${
          d.formats[Math.min(d.items.length, d.formats.length - 1)]
        }…`;
  return (
    <div className="space-y-4 rounded-xl border p-4">
      <div className="flex items-center gap-2">
        <p className="font-medium">{d.subject}</p>
        <Badge variant={STATUS_VARIANT[d.status] ?? "outline"}>{d.status}</Badge>
        {d.plan ? (
          <Badge variant="outline" title="Formats chosen by the editorial planner">
            <Wand2Icon data-icon="inline-start" />
            auto
          </Badge>
        ) : null}
        <span className="text-xs text-muted-foreground">{relativeTime(d.created_at)}</span>
        <CampaignActions d={d} onGone={() => setGone(true)} />
      </div>
      {d.plan ? (
        <div className="rounded-lg border bg-muted/20 p-2.5 text-xs">
          <div className="flex flex-wrap items-center gap-1.5">
            <Wand2Icon className="size-3.5 text-muted-foreground" />
            <Badge variant={SIGNIFICANCE_VARIANT[d.plan.significance_label] ?? "outline"}>
              {d.plan.significance_label} · {d.plan.significance}/5
            </Badge>
            {d.plan.picks.map((p) => (
              <Badge key={p.format} variant="outline" title={p.reason.slice(0, 300)}>
                {p.media === "stock_video" ? <FilmIcon data-icon="inline-start" /> : null}
                {p.media === "stock_photo" ? <ImageIcon data-icon="inline-start" /> : null}
                {p.format}
              </Badge>
            ))}
            {d.plan.angle ? <span className="font-medium">{d.plan.angle}</span> : null}
          </div>
          {d.plan.rationale ? <p className="mt-1 text-muted-foreground">{d.plan.rationale}</p> : null}
        </div>
      ) : null}
      {d.error ? <p className="text-sm text-destructive">{d.error}</p> : null}
      {d.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{progress ?? "No items."}</p>
      ) : (
        <div className="space-y-4">
          {d.items.map((item) => {
            const canEdit = item.status !== "published";
            return (
              <div key={item.id} className="space-y-2 rounded-lg border bg-muted/20 p-3">
                <div className="flex items-center gap-2">
                  <Badge variant="outline">{item.format}</Badge>
                  <Badge variant={STATUS_VARIANT[item.status] ?? "outline"}>{item.status}</Badge>
                  {item.edited ? <Badge variant="outline">edited</Badge> : null}
                  {canEdit ? (
                    <Button
                      size="xs"
                      variant="ghost"
                      className="ml-auto"
                      onClick={() => setEditing((e) => (e === item.id ? null : item.id))}
                    >
                      {editing === item.id ? "Close editor" : "Edit"}
                    </Button>
                  ) : null}
                </div>
                <ItemPreview item={item} />
                <ItemMeta item={item} />
                {item.error ? <p className="text-xs text-destructive">{item.error}</p> : null}
                <TrustPanelForItem item={item} />
                <SourcesPanel item={item} />
                {editing === item.id ? (
                  <ContentEditor item={item} onRegenerating={() => setRegenUntil(Date.now() + 50_000)} />
                ) : null}
                <ItemActions item={item} />
              </div>
            );
          })}
          {progress && d.items.length < d.formats.length ? (
            <p className="text-sm text-muted-foreground">{progress}</p>
          ) : null}
        </div>
      )}
    </div>
  );
}

function CampaignList({ workspaceId }: { workspaceId?: number }) {
  const campaigns = useQuery({
    queryKey: ["campaigns", workspaceId ?? null],
    queryFn: () => listCampaigns(workspaceId),
  });

  if (campaigns.isPending) return <Skeleton className="h-24 w-full" />;
  if (campaigns.isError) return <QueryError error={campaigns.error} onRetry={() => void campaigns.refetch()} />;
  if (campaigns.data.items.length === 0) {
    return (
      <EmptyState
        icon={FilmIcon}
        title="No content yet"
        description="Pick a format above to generate your first drafts."
      />
    );
  }
  return (
    <div className="space-y-4">
      {campaigns.data.items.map((c) => (
        <CampaignCard key={c.id} id={c.id} />
      ))}
    </div>
  );
}

/** The full content pipeline: create + review/edit/schedule/publish. Scoped to
 * a workspace when ``workspaceId`` is given, otherwise topic-driven (the
 * standalone /content page). */
export function ContentStudio({ workspaceId }: { workspaceId?: number }) {
  return (
    <>
      <CreateCampaign workspaceId={workspaceId} />
      <CampaignList workspaceId={workspaceId} />
    </>
  );
}
