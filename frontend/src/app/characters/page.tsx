"use client";

import { useState } from "react";
import { DramaIcon, PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { CharacterForm } from "@/components/characters/CharacterForm";
import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";
import { QueryError } from "@/components/shared/QueryError";
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
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { Character, CharacterInput } from "@/lib/api";
import {
  useCharacters,
  useCreateCharacter,
  useDeleteCharacter,
  useUpdateCharacter,
} from "@/lib/queries";

export default function CharactersPage() {
  const characters = useCharacters();
  const create = useCreateCharacter();
  const update = useUpdateCharacter();
  const del = useDeleteCharacter();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Character | null>(null);
  const [removing, setRemoving] = useState<Character | null>(null);

  const onCreate = (body: CharacterInput) =>
    create.mutate(body, {
      onSuccess: () => {
        toast.success("Character created");
        setAdding(false);
      },
      onError: (e) => toast.error("Could not create", { description: e.message }),
    });

  const onUpdate = (body: CharacterInput) => {
    if (!editing) return;
    update.mutate(
      { id: editing.id, body },
      {
        onSuccess: () => {
          toast.success("Character updated");
          setEditing(null);
        },
        onError: (e) => toast.error("Could not update", { description: e.message }),
      },
    );
  };

  const onDelete = () => {
    if (!removing) return;
    del.mutate(removing.id, {
      onSuccess: () => {
        toast.success("Character deleted");
        setRemoving(null);
      },
      onError: (e) => toast.error("Could not delete", { description: e.message }),
    });
  };

  return (
    <>
      <PageHeader
        title="Characters"
        description="Personas your reels and posts are written and narrated as — pick one per channel."
        actions={
          <Button size="sm" onClick={() => setAdding(true)}>
            <PlusIcon data-icon="inline-start" />
            Add character
          </Button>
        }
      />

      {characters.isPending ? (
        <Skeleton className="h-64 w-full" />
      ) : characters.isError ? (
        <QueryError error={characters.error} onRetry={() => void characters.refetch()} />
      ) : characters.data.length === 0 ? (
        <EmptyState
          icon={DramaIcon}
          title="No characters yet"
          description="Create a persona — a name, personality, speaking style and voice — to front your content."
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Personality</TableHead>
              <TableHead>Voice</TableHead>
              <TableHead className="w-24 text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {characters.data.map((c) => (
              <TableRow key={c.id}>
                <TableCell className="font-medium">{c.name}</TableCell>
                <TableCell className="max-w-md truncate text-muted-foreground">
                  {c.description || "—"}
                </TableCell>
                <TableCell>
                  {c.voice_id ? (
                    <span className="flex items-center gap-1">
                      <Badge variant="outline">{c.voice_id}</Badge>
                      <VoiceAuditionButton
                        voiceId={c.voice_id}
                        text={c.sample_line || `Hi, I'm ${c.name}.`}
                        size="icon-xs"
                      />
                    </span>
                  ) : (
                    <span className="text-xs text-muted-foreground">none</span>
                  )}
                </TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon-sm" aria-label="Edit" onClick={() => setEditing(c)}>
                    <PencilIcon />
                  </Button>
                  <Button variant="ghost" size="icon-sm" aria-label="Delete" onClick={() => setRemoving(c)}>
                    <Trash2Icon />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Dialog open={adding} onOpenChange={setAdding}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Add character</DialogTitle>
            <DialogDescription>Define a persona to front your content.</DialogDescription>
          </DialogHeader>
          <CharacterForm submitting={create.isPending} submitLabel="Add character" onSubmit={onCreate} />
        </DialogContent>
      </Dialog>

      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit character</DialogTitle>
            <DialogDescription>Update this persona.</DialogDescription>
          </DialogHeader>
          {editing ? (
            <CharacterForm
              key={editing.id}
              character={editing}
              submitting={update.isPending}
              submitLabel="Save changes"
              onSubmit={onUpdate}
            />
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={removing !== null} onOpenChange={(o) => !o && setRemoving(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete “{removing?.name}”?</DialogTitle>
            <DialogDescription>
              Any workspace channel using it as its default falls back to its channel default.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={del.isPending} onClick={onDelete}>
              Delete
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
