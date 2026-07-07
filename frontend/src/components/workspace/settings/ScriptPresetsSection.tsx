"use client";

import { useState } from "react";
import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { ScriptPresetForm } from "@/components/workspace/settings/ScriptPresetForm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { ScriptPreset, ScriptPresetInput } from "@/lib/api";
import {
  useCreateScriptPreset,
  useDeleteScriptPreset,
  useScriptPresets,
  useUpdateScriptPreset,
} from "@/lib/queries";

/** Script-type presets: read-only built-ins + custom CRUD. */
export function ScriptPresetsSection() {
  const presets = useScriptPresets();
  const create = useCreateScriptPreset();
  const update = useUpdateScriptPreset();
  const del = useDeleteScriptPreset();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<ScriptPreset | null>(null);

  const onCreate = (body: ScriptPresetInput) =>
    create.mutate(body, {
      onSuccess: () => {
        toast.success("Script type created");
        setAdding(false);
      },
      onError: (e) => toast.error("Could not create", { description: e.message }),
    });

  const onUpdate = (body: ScriptPresetInput) => {
    if (!editing) return;
    update.mutate(
      { id: editing.id, body },
      {
        onSuccess: () => {
          toast.success("Script type updated");
          setEditing(null);
        },
        onError: (e) => toast.error("Could not update", { description: e.message }),
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          Script types
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            <PlusIcon data-icon="inline-start" />
            Add
          </Button>
        </CardTitle>
        <CardDescription>
          The shapes your content can be written in — built-ins plus your own presets. Pick one per
          campaign.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-1.5">
        {(presets.data ?? []).map((p) => (
          <div key={p.id} className="flex items-center gap-2 rounded-md border px-2.5 py-1.5">
            <span className="font-medium">{p.name}</span>
            {p.builtin ? (
              <Badge variant="outline">built-in</Badge>
            ) : (
              <span className="ml-auto flex items-center gap-1">
                <Button variant="ghost" size="icon-xs" aria-label="Edit" onClick={() => setEditing(p)}>
                  <PencilIcon />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Delete"
                  onClick={() =>
                    del.mutate(p.id, {
                      onError: (e) => toast.error("Could not delete", { description: e.message }),
                    })
                  }
                >
                  <Trash2Icon />
                </Button>
              </span>
            )}
          </div>
        ))}
      </CardContent>

      <Dialog open={adding} onOpenChange={setAdding}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Add script type</DialogTitle>
          </DialogHeader>
          <ScriptPresetForm submitting={create.isPending} submitLabel="Add" onSubmit={onCreate} />
        </DialogContent>
      </Dialog>

      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit script type</DialogTitle>
          </DialogHeader>
          {editing ? (
            <ScriptPresetForm
              key={editing.id}
              preset={editing}
              submitting={update.isPending}
              submitLabel="Save changes"
              onSubmit={onUpdate}
            />
          ) : null}
        </DialogContent>
      </Dialog>
    </Card>
  );
}
