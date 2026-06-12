"use client";

import Link from "next/link";
import { CircleAlertIcon, FileTextIcon, Loader2Icon, Share2Icon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError, shareConfirmationDocuments } from "@/lib/api";
import type { ShareDocumentRef } from "@/lib/api";
import {
  useShareAnalysis,
  useShareDocument,
  useShareInvestigation,
} from "@/lib/queries";

/**
 * Share confirmation dialogs (design §1 share cascade, §6). Sharing is
 * deliberately friction-ful: it cannot be undone once findings/enrichment
 * compound into the shared knowledge base, so both dialogs spell out the
 * consequence and surface the backend's 409 explanation inline when the
 * share is refused.
 */

/** Inline rendering of a 409/other share failure inside the dialog. */
function ShareError({ error }: { error: Error | null }) {
  if (!error) return null;
  const detail =
    error instanceof ApiError && error.status === 409
      ? error.detail
      : error.message;
  return (
    <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2">
      <CircleAlertIcon
        className="mt-0.5 size-4 shrink-0 text-destructive"
        aria-hidden
      />
      <p className="text-xs leading-snug text-destructive">{detail}</p>
    </div>
  );
}

/** The cascade list: private documents that sharing will auto-share. */
function CascadeDocumentList({ documents }: { documents: ShareDocumentRef[] }) {
  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium">
        Sharing also shares {documents.length} private document
        {documents.length === 1 ? "" : "s"} cited as evidence:
      </p>
      <ul className="max-h-40 space-y-1 overflow-y-auto rounded-lg border bg-muted/30 px-3 py-2">
        {documents.map((doc) => (
          <li key={doc.id} className="flex items-center gap-1.5 text-xs">
            <FileTextIcon
              className="size-3.5 shrink-0 text-muted-foreground"
              aria-hidden
            />
            <Link
              href={`/documents/${doc.id}`}
              className="truncate text-primary underline-offset-4 hover:underline"
            >
              {doc.title ?? `Document #${doc.id}`}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Confirmation for sharing a private analysis/investigation, speaking the
 * backend's two-step PATCH protocol (DossierVisibilityUpdate): the first
 * Share click probes with `confirm_documents=false`; if the dossier cites
 * the owner's private documents, the 409 confirmation list renders here and
 * a second click re-PATCHes with `confirm_documents=true` to run the
 * cascade. Any other 409 (foreign private evidence — defensive — or a
 * shared→private downgrade) renders its explanation inline.
 */
export function ShareDossierDialog({
  kind,
  id,
  open,
  onOpenChange,
}: {
  kind: "analysis" | "investigation";
  id: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const shareAnalysis = useShareAnalysis();
  const shareInvestigation = useShareInvestigation();
  const share = kind === "analysis" ? shareAnalysis : shareInvestigation;

  // Derived, not stored: while the latest failure is a confirmation 409 the
  // dialog is in its "confirm the cascade" round; share.reset() clears it.
  const confirmationDocs = shareConfirmationDocuments(share.error);
  const needsConfirm = confirmationDocs !== null;

  const close = (next: boolean) => {
    if (share.isPending) return;
    if (!next) share.reset(); // a stale 409 must not greet the next open
    onOpenChange(next);
  };

  const onShare = () =>
    share.mutate(
      { id, confirm: needsConfirm },
      {
        onSuccess: () => {
          toast.success("Shared with everyone", {
            description: `This ${kind} is now visible to all members.`,
          });
          share.reset();
          onOpenChange(false);
        },
        onError: (error) => {
          // The confirmation 409 is the expected second round, and other
          // 409s carry an inline explanation; everything else toasts.
          if (shareConfirmationDocuments(error) !== null) return;
          if (!(error instanceof ApiError && error.status === 409)) {
            toast.error("Could not share", { description: error.message });
          }
        },
      },
    );

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Share this {kind}?</DialogTitle>
          <DialogDescription>
            Every member will be able to read it, and its grounded findings
            join the community knowledge graph. Sharing cannot be undone.
          </DialogDescription>
        </DialogHeader>

        {needsConfirm ? (
          <CascadeDocumentList documents={confirmationDocs} />
        ) : (
          <p className="text-xs leading-snug text-muted-foreground">
            Private documents you own that this {kind} cites as evidence will
            be shared along with it — you&apos;ll be asked to confirm the list
            first.
          </p>
        )}

        <ShareError error={needsConfirm ? null : share.error} />

        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
          <Button disabled={share.isPending} onClick={onShare}>
            {share.isPending ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : (
              <Share2Icon data-icon="inline-start" />
            )}
            {needsConfirm
              ? `Share all (${confirmationDocs.length + 1})`
              : "Share"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Confirmation for sharing a private document (upload/pasted text). */
export function ShareDocumentDialog({
  id,
  title,
  open,
  onOpenChange,
}: {
  id: number;
  title: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const share = useShareDocument();

  const close = (next: boolean) => {
    if (share.isPending) return;
    if (!next) share.reset();
    onOpenChange(next);
  };

  const onShare = () =>
    share.mutate(id, {
      onSuccess: () => {
        toast.success("Shared with everyone", {
          description: title ?? `Document #${id}`,
        });
        onOpenChange(false);
      },
      onError: (error) => {
        if (!(error instanceof ApiError && error.status === 409)) {
          toast.error("Could not share", { description: error.message });
        }
      },
    });

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Share this document?</DialogTitle>
          <DialogDescription>
            Every member will be able to read{" "}
            {title ? <span className="font-medium">“{title}”</span> : "it"},
            and it becomes eligible for knowledge extraction (entities, claims,
            events). Sharing cannot be undone.
          </DialogDescription>
        </DialogHeader>

        <ShareError error={share.error} />

        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
          <Button disabled={share.isPending} onClick={onShare}>
            {share.isPending ? (
              <Loader2Icon className="animate-spin" data-icon="inline-start" />
            ) : (
              <Share2Icon data-icon="inline-start" />
            )}
            Share
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
