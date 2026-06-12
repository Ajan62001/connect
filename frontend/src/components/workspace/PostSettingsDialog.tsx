"use client";

import { useState } from "react";
import { Loader2Icon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  VisibilityToggle,
} from "@/components/shared/Visibility";
import { Button } from "@/components/ui/button";
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
import type { PostSettings, Visibility } from "@/lib/api";
import { queryKeys, useUpdateWorkspace, useWorkspacePostSettings } from "@/lib/queries";

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
  const update = useUpdateWorkspace(workspaceId);
  const queryClient = useQueryClient();
  // form holds only the fields the user has touched; everything else falls
  // back to the effective settings — no syncing props into state.
  const [form, setForm] = useState<Partial<PostSettings>>({});

  const get = <K extends keyof PostSettings>(k: K): PostSettings[K] | undefined =>
    k in form ? form[k] : eff.data?.[k];
  const set = <K extends keyof PostSettings>(k: K, v: PostSettings[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const onSaved = () => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.workspacePostSettings(workspaceId),
    });
    setForm({});
    onOpenChange(false);
  };

  const save = () => {
    if (!eff.data) return;
    update.mutate(
      { post_settings: { ...eff.data, ...form } },
      {
        onSuccess: () => {
          toast.success("Post settings saved for this workspace");
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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Post settings</DialogTitle>
          <DialogDescription>
            How this workspace generates Instagram posts. Unset fields use the
            global default.
          </DialogDescription>
        </DialogHeader>

        {eff.isPending ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" />
            Loading…
          </div>
        ) : eff.data ? (
          <div className="space-y-3">
            <div className="space-y-1">
              <Label>Tone</Label>
              <Input
                value={get("tone") ?? ""}
                onChange={(e) => set("tone", e.target.value)}
                placeholder="neutral, factual, engaging"
              />
            </div>
            <div className="flex gap-3">
              <div className="flex-1 space-y-1">
                <Label>Hashtags</Label>
                <Input
                  type="number"
                  value={String(get("hashtag_count") ?? 8)}
                  onChange={(e) =>
                    set("hashtag_count", Number(e.target.value) || 0)
                  }
                />
              </div>
              <div className="flex-1 space-y-1">
                <Label>Caption max chars</Label>
                <Input
                  type="number"
                  value={String(get("caption_max_chars") ?? 400)}
                  onChange={(e) =>
                    set("caption_max_chars", Number(e.target.value) || 0)
                  }
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label>Brand handle</Label>
              <Input
                value={get("brand_handle") ?? ""}
                onChange={(e) => set("brand_handle", e.target.value)}
                placeholder="@yourbrand (optional)"
              />
            </div>
            <div className="flex gap-3">
              <div className="flex-1 space-y-1">
                <Label>Card accent</Label>
                <div className="flex items-center gap-2">
                  <span
                    className="size-6 shrink-0 rounded border"
                    style={{ backgroundColor: get("card_accent") ?? "#38bdf8" }}
                  />
                  <Input
                    value={get("card_accent") ?? ""}
                    onChange={(e) => set("card_accent", e.target.value)}
                    placeholder="#38bdf8"
                  />
                </div>
              </div>
              <div className="flex-1 space-y-1">
                <Label>Sign-off</Label>
                <Input
                  value={get("sign_off") ?? ""}
                  onChange={(e) => set("sign_off", e.target.value)}
                  placeholder="via connect"
                />
              </div>
            </div>
            <VisibilityToggle
              value={(get("default_visibility") ?? "shared") as Visibility}
              onChange={(v) => set("default_visibility", v)}
              kind="post"
            />
          </div>
        ) : null}

        <DialogFooter className="gap-2 sm:justify-between">
          <Button
            variant="ghost"
            size="sm"
            onClick={resetToGlobal}
            disabled={update.isPending}
          >
            Use global default
          </Button>
          <Button onClick={save} disabled={!eff.data || update.isPending}>
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
