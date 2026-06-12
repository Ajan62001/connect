"use client";

import { useState } from "react";
import { Loader2Icon, SaveIcon, Settings2Icon } from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
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
import { ApiError } from "@/lib/api";
import type { AdminSettings, AdminSettingsUpdate } from "@/lib/api";
import { useAdminSettings, useUpdateAdminSettings } from "@/lib/queries";

type SettingKey = keyof AdminSettingsUpdate;

const FIELDS: {
  key: SettingKey;
  label: string;
  description: string;
}[] = [
  {
    key: "global_daily_budget_usd",
    label: "Global daily backstop (USD)",
    description:
      "Hard deployment-wide ceiling across ALL users and system jobs. Nothing spends past this, whatever the per-user caps say.",
  },
  {
    key: "daily_llm_budget_usd",
    label: "General envelope (USD)",
    description:
      "Daily envelope for general work (enrichment, analyses, briefs' LLM-free paths aside) — also the default general cap for admins.",
  },
  {
    key: "investigation_daily_budget_usd",
    label: "Investigation envelope (USD)",
    description:
      "Separate daily envelope for investigation runs — also the default investigation cap for admins.",
  },
  {
    key: "member_daily_budget_usd",
    label: "Member daily budget (USD)",
    description:
      "Default general cap per member per day (analyses, promotions, enrichment they trigger). Per-user overrides on the Users tab win.",
  },
  {
    key: "member_investigation_daily_budget_usd",
    label: "Member investigation budget (USD)",
    description:
      "Default per-member daily cap for investigations — they are deliberate, costlier runs.",
  },
];

function SettingsForm({ settings }: { settings: AdminSettings }) {
  const update = useUpdateAdminSettings();
  // Draft keyed by setting; initialized from the server snapshot once.
  const [draft, setDraft] = useState<Record<SettingKey, string>>(() => ({
    global_daily_budget_usd:
      settings.global_daily_budget_usd?.toString() ?? "",
    daily_llm_budget_usd: settings.daily_llm_budget_usd?.toString() ?? "",
    investigation_daily_budget_usd:
      settings.investigation_daily_budget_usd?.toString() ?? "",
    member_daily_budget_usd:
      settings.member_daily_budget_usd?.toString() ?? "",
    member_investigation_daily_budget_usd:
      settings.member_investigation_daily_budget_usd?.toString() ?? "",
  }));

  // Only changed, valid, non-negative numbers go on the wire.
  const changes: AdminSettingsUpdate = {};
  let invalid = false;
  for (const { key } of FIELDS) {
    const raw = draft[key].trim();
    if (raw === "") continue; // blank = leave as-is (env/seed default)
    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed < 0) {
      invalid = true;
      continue;
    }
    if (parsed !== settings[key]) changes[key] = parsed;
  }
  const dirty = Object.keys(changes).length > 0;

  const save = () => {
    if (!dirty || invalid) return;
    update.mutate(changes, {
      onSuccess: () =>
        toast.success("Budgets updated", {
          description:
            "New ceilings apply from the next governed call — no redeploy needed.",
        }),
      onError: (error) =>
        toast.error("Could not save settings", { description: error.message }),
    });
  };

  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle className="text-base">Deployment budgets</CardTitle>
        <CardDescription>
          Stored in <code>app_setting</code> — these win over the environment
          defaults the moment you save, and per-user overrides (Users tab) win
          over these. Admin accounts keep the deployment&apos;s admin caps.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {FIELDS.map(({ key, label, description }) => (
          <div key={key} className="space-y-1.5">
            <Label htmlFor={key}>{label}</Label>
            <div className="flex items-center gap-1.5">
              <span className="text-sm text-muted-foreground">$</span>
              <Input
                id={key}
                value={draft[key]}
                onChange={(e) =>
                  setDraft((prev) => ({ ...prev, [key]: e.target.value }))
                }
                inputMode="decimal"
                placeholder={settings[key] === null ? "env default" : undefined}
                className="w-32 text-right tabular-nums"
              />
              <span className="text-xs text-muted-foreground">/ day</span>
            </div>
            <p className="text-xs leading-snug text-muted-foreground">
              {description}
            </p>
          </div>
        ))}
        <div className="flex items-center justify-end gap-3">
          {invalid ? (
            <span className="text-xs text-destructive">
              Budgets must be non-negative numbers.
            </span>
          ) : null}
          <Button disabled={!dirty || invalid || update.isPending} onClick={save}>
            {update.isPending ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : (
              <SaveIcon data-icon="inline-start" />
            )}
            Save budgets
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export default function AdminSettingsPage() {
  const settings = useAdminSettings();

  if (settings.isPending) {
    return <Skeleton className="h-72 w-full max-w-2xl rounded-xl" />;
  }
  if (settings.isError) {
    if (settings.error instanceof ApiError && settings.error.status === 404) {
      return (
        <EmptyState
          icon={Settings2Icon}
          title="Settings API not available yet"
          description="The backend hasn't shipped /api/admin/settings; budget knobs appear here once it does."
        />
      );
    }
    return (
      <QueryError
        error={settings.error}
        onRetry={() => void settings.refetch()}
      />
    );
  }

  // Key the form by the server snapshot so a refetch after save re-seeds
  // the drafts (simplest correct reset without effect plumbing).
  return (
    <SettingsForm
      key={JSON.stringify(settings.data)}
      settings={settings.data}
    />
  );
}
