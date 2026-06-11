"use client";

import { useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { CircleCheckIcon, CircleXIcon, Loader2Icon } from "lucide-react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  CredibilityTier,
  Source,
  SourceCreate,
  SourceTestResult,
  SourceType,
} from "@/lib/api";
import { TIER_LABELS } from "@/components/sources/TierDots";
import { useTestSource } from "@/lib/queries";

// ---------------------------------------------------------------------------
// Schema — discriminated union on source type. rss, twitter, telegram and
// manual are configurable; the remaining adapter types render disabled.
// Every variant carries every field (uniform react-hook-form state); only
// the active variant constrains its own fields.
// ---------------------------------------------------------------------------

/** X handle: 1-15 word characters (the leading @ is stripped on parse). */
const HANDLE_RE = /^[A-Za-z0-9_]{1,15}$/;
/** Telegram public username: letter first, 5-32 chars (t.me rules). */
const CHANNEL_RE = /^[A-Za-z][A-Za-z0-9_]{4,31}$/;

/** Textarea (one handle per line) -> cleaned handle list ('@' stripped). */
function parseHandles(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim().replace(/^@/, ""))
    .filter((line) => line.length > 0);
}

function normalizeChannel(raw: string): string {
  return raw.trim().replace(/^@/, "");
}

const baseShape = {
  name: z.string().min(1, "Name is required"),
  credibility_tier: z.enum(["1", "2", "3", "4"]),
  notes: z.string(),
};

const pollIntervalSchema = z
  .string()
  .regex(/^\d+$/, "Whole minutes only")
  .refine((value) => Number(value) >= 1, "At least 1 minute");

const handlesSchema = z.string().superRefine((value, ctx) => {
  const handles = parseHandles(value);
  if (handles.length === 0) {
    ctx.addIssue({ code: "custom", message: "Add at least one handle" });
    return;
  }
  if (handles.length > 50) {
    ctx.addIssue({ code: "custom", message: "At most 50 handles" });
  }
  const bad = handles.find((handle) => !HANDLE_RE.test(handle));
  if (bad !== undefined) {
    ctx.addIssue({
      code: "custom",
      message: `Invalid handle “${bad}” — 1-15 letters, digits or _`,
    });
  }
});

const channelSchema = z.string().refine(
  (value) => CHANNEL_RE.test(normalizeChannel(value)),
  "Public channel username (5-32 chars), e.g. PIB_FactCheck",
);

const sourceFormSchema = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("rss"),
    ...baseShape,
    feed_url: z
      .string()
      .regex(/^https?:\/\/\S+$/, "Enter a valid http(s) feed URL"),
    poll_interval: pollIntervalSchema,
    handles: z.string(),
    channel: z.string(),
  }),
  z.object({
    type: z.literal("twitter"),
    ...baseShape,
    feed_url: z.string(),
    poll_interval: pollIntervalSchema,
    handles: handlesSchema,
    channel: z.string(),
  }),
  z.object({
    type: z.literal("telegram"),
    ...baseShape,
    feed_url: z.string(),
    poll_interval: pollIntervalSchema,
    handles: z.string(),
    channel: channelSchema,
  }),
  z.object({
    type: z.literal("manual"),
    ...baseShape,
    feed_url: z.string(),
    poll_interval: z.string(),
    handles: z.string(),
    channel: z.string(),
  }),
]);

export type SourceFormValues = z.infer<typeof sourceFormSchema>;

type ConfigurableType = SourceFormValues["type"];

const CONFIGURABLE_TYPES: ConfigurableType[] = [
  "rss",
  "twitter",
  "telegram",
  "manual",
];

function isConfigurable(value: string): value is ConfigurableType {
  return (CONFIGURABLE_TYPES as string[]).includes(value);
}

/** Types POST /sources/test can exercise. */
const TESTABLE_TYPES: ConfigurableType[] = ["rss", "twitter", "telegram"];

const TYPE_ITEMS: { value: SourceType; label: string; disabled: boolean }[] = [
  { value: "rss", label: "RSS feed", disabled: false },
  { value: "twitter", label: "X / Twitter (twitterapi.io)", disabled: false },
  { value: "telegram", label: "Telegram public channel", disabled: false },
  { value: "manual", label: "Manual (uploads / pastes)", disabled: false },
  { value: "scrape", label: "Scraper — coming soon", disabled: true },
  { value: "api", label: "API — coming soon", disabled: true },
  { value: "search", label: "Search — coming soon", disabled: true },
];

const TYPE_LABELS: Record<string, React.ReactNode> = Object.fromEntries(
  TYPE_ITEMS.map((item) => [item.value, item.label]),
);

const TIER_ITEMS: Record<string, React.ReactNode> = {
  "1": TIER_LABELS[1],
  "2": TIER_LABELS[2],
  "3": TIER_LABELS[3],
  "4": TIER_LABELS[4],
};

function toConfig(values: SourceFormValues): Record<string, unknown> {
  switch (values.type) {
    case "rss":
      return {
        feed_url: values.feed_url.trim(),
        poll_interval_minutes: Number(values.poll_interval),
      };
    case "twitter":
      return {
        handles: parseHandles(values.handles),
        poll_interval_minutes: Number(values.poll_interval),
      };
    case "telegram":
      return {
        channel: normalizeChannel(values.channel),
        poll_interval_minutes: Number(values.poll_interval),
      };
    default:
      return {};
  }
}

function toPayload(values: SourceFormValues): SourceCreate {
  return {
    name: values.name.trim(),
    type: values.type,
    config: toConfig(values),
    credibility_tier: Number(values.credibility_tier) as CredibilityTier,
    notes: values.notes.trim() ? values.notes.trim() : null,
  };
}

function defaultsFrom(source: Source | undefined): SourceFormValues {
  const config = source?.config ?? {};
  const interval =
    config.poll_interval_minutes ?? config.poll_interval ?? undefined;
  return {
    type: source && isConfigurable(source.type) ? source.type : source ? "manual" : "rss",
    name: source?.name ?? "",
    credibility_tier: String(source?.credibility_tier ?? 2) as
      | "1"
      | "2"
      | "3"
      | "4",
    notes: source?.notes ?? "",
    feed_url: typeof config.feed_url === "string" ? config.feed_url : "",
    poll_interval: interval !== undefined ? String(interval) : "30",
    handles: Array.isArray(config.handles) ? config.handles.join("\n") : "",
    channel: typeof config.channel === "string" ? config.channel : "",
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SourceForm({
  source,
  submitting,
  submitLabel,
  onSubmit,
}: {
  /** When present the form edits this source (type becomes read-only). */
  source?: Source;
  submitting: boolean;
  submitLabel: string;
  onSubmit: (payload: SourceCreate) => void;
}) {
  const form = useForm<SourceFormValues>({
    resolver: zodResolver(sourceFormSchema),
    defaultValues: defaultsFrom(source),
  });
  const test = useTestSource();
  const [testResult, setTestResult] = useState<SourceTestResult | null>(null);

  const type = form.watch("type");
  const errors = form.formState.errors;
  const testable = (TESTABLE_TYPES as string[]).includes(type);

  async function runTest() {
    const fields: ("feed_url" | "poll_interval" | "handles" | "channel")[] =
      type === "rss"
        ? ["feed_url", "poll_interval"]
        : type === "twitter"
          ? ["handles", "poll_interval"]
          : ["channel", "poll_interval"];
    const valid = await form.trigger(fields);
    if (!valid) return;
    const values = form.getValues();
    setTestResult(null);
    test.mutate(
      { type: values.type, config: toConfig(values) },
      {
        onSuccess: setTestResult,
        onError: (error) =>
          setTestResult({ ok: false, sample_items: [], error: error.message }),
      },
    );
  }

  return (
    <form
      onSubmit={form.handleSubmit((values) => onSubmit(toPayload(values)))}
      noValidate
    >
      <FieldGroup className="gap-4">
        <Field>
          <FieldLabel htmlFor="source-name">Name</FieldLabel>
          <Input
            id="source-name"
            placeholder="e.g. RBI Press Releases"
            {...form.register("name")}
          />
          <FieldError errors={[errors.name]} />
        </Field>

        <Field>
          <FieldLabel>Type</FieldLabel>
          <Controller
            control={form.control}
            name="type"
            render={({ field }) => (
              <Select
                items={TYPE_LABELS}
                value={field.value}
                onValueChange={(value) => {
                  if (typeof value === "string" && isConfigurable(value)) {
                    field.onChange(value);
                    setTestResult(null);
                  }
                }}
                disabled={source !== undefined}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Select a type" />
                </SelectTrigger>
                <SelectContent>
                  {TYPE_ITEMS.map((item) => (
                    <SelectItem
                      key={item.value}
                      value={item.value}
                      disabled={item.disabled}
                    >
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          {source ? (
            <FieldDescription>
              The type of an existing source cannot change.
            </FieldDescription>
          ) : null}
        </Field>

        {type === "rss" ? (
          <Field>
            <FieldLabel htmlFor="source-feed-url">Feed URL</FieldLabel>
            <Input
              id="source-feed-url"
              type="url"
              placeholder="https://www.rbi.org.in/pressreleases_rss.xml"
              {...form.register("feed_url")}
            />
            <FieldError errors={[errors.feed_url]} />
          </Field>
        ) : null}

        {type === "twitter" ? (
          <Field>
            <FieldLabel htmlFor="source-handles">
              Handles (one per line, without @)
            </FieldLabel>
            <Textarea
              id="source-handles"
              rows={5}
              placeholder={"PIB_India\nPIBFactCheck\nRBI"}
              {...form.register("handles")}
            />
            <FieldError errors={[errors.handles]} />
            <FieldDescription>
              Up to 50 handles, polled together in one batched twitterapi.io
              search call. Requires TWITTERAPI_IO_API_KEY on the backend;
              without it polls cleanly skip.
            </FieldDescription>
          </Field>
        ) : null}

        {type === "telegram" ? (
          <Field>
            <FieldLabel htmlFor="source-channel">Channel</FieldLabel>
            <Input
              id="source-channel"
              placeholder="PIB_FactCheck"
              {...form.register("channel")}
            />
            <FieldError errors={[errors.channel]} />
            <FieldDescription>
              Public channel username — read free via the t.me/s preview, no
              API key needed.
            </FieldDescription>
          </Field>
        ) : null}

        {type === "manual" ? (
          <FieldDescription>
            Manual sources have no configuration — they group documents you
            paste or upload yourself.
          </FieldDescription>
        ) : (
          <Field>
            <FieldLabel htmlFor="source-poll-interval">
              Poll interval (minutes)
            </FieldLabel>
            <Input
              id="source-poll-interval"
              inputMode="numeric"
              className="w-32"
              {...form.register("poll_interval")}
            />
            <FieldError errors={[errors.poll_interval]} />
          </Field>
        )}

        <Field>
          <FieldLabel>Credibility tier</FieldLabel>
          <Controller
            control={form.control}
            name="credibility_tier"
            render={({ field }) => (
              <Select
                items={TIER_ITEMS}
                value={field.value}
                onValueChange={(value) => {
                  if (value) field.onChange(String(value));
                }}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Select a tier" />
                </SelectTrigger>
                <SelectContent>
                  {([1, 2, 3, 4] as const).map((tier) => (
                    <SelectItem key={tier} value={String(tier)}>
                      {TIER_LABELS[tier]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          <FieldDescription>
            Tier weights evidence in later phases; tier 1 is official/primary.
          </FieldDescription>
        </Field>

        <Field>
          <FieldLabel htmlFor="source-notes">Notes (optional)</FieldLabel>
          <Textarea
            id="source-notes"
            placeholder="Anything worth remembering about this source…"
            {...form.register("notes")}
          />
        </Field>

        {testable ? (
          <div className="space-y-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={test.isPending}
              onClick={() => void runTest()}
            >
              {test.isPending ? (
                <Loader2Icon className="animate-spin" data-icon="inline-start" />
              ) : null}
              Test source
            </Button>
            {testResult ? (
              testResult.ok ? (
                <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3 text-sm">
                  <p className="mb-1.5 flex items-center gap-1.5 font-medium text-emerald-700 dark:text-emerald-400">
                    <CircleCheckIcon className="size-4" />
                    Source looks good
                    {testResult.sample_items.length > 0
                      ? ` — ${testResult.sample_items.length} sample item${testResult.sample_items.length === 1 ? "" : "s"}`
                      : ""}
                  </p>
                  <ul className="space-y-1 text-muted-foreground">
                    {testResult.sample_items.slice(0, 5).map((item, index) => (
                      <li key={index} className="truncate">
                        {item.title}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <div className="flex items-start gap-1.5 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                  <CircleXIcon className="mt-0.5 size-4 shrink-0" />
                  <span>{testResult.error ?? "Test failed"}</span>
                </div>
              )
            ) : null}
          </div>
        ) : null}

        <Button type="submit" disabled={submitting}>
          {submitting ? (
            <Loader2Icon className="animate-spin" data-icon="inline-start" />
          ) : null}
          {submitLabel}
        </Button>
      </FieldGroup>
    </form>
  );
}
