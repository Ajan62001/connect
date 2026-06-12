"use client";

import { useState } from "react";
import { CheckIcon, LinkIcon, MailPlusIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import type { Invite } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useCreateInvite, useDeleteInvite, useInvites } from "@/lib/queries";

/**
 * Copies the deployment's sign-in link for an invited email. Invites are an
 * email allowlist, not tokens — the link is the same for everyone; what the
 * invite changes is that this email's first Google sign-in now succeeds.
 */
function CopyLinkButton({ email }: { email: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    const link = `${window.location.origin}/signin`;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      setTimeout(() => setCopied(false), 2_000);
      toast.success("Sign-in link copied", {
        description: `Send it to ${email} — their Google sign-in is now on the allowlist.`,
      });
    } catch {
      toast.error("Could not copy", { description: link });
    }
  };

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`Copy sign-in link for ${email}`}
            onClick={() => void copy()}
          />
        }
      >
        {copied ? <CheckIcon /> : <LinkIcon />}
      </TooltipTrigger>
      <TooltipContent>Copy sign-in link</TooltipContent>
    </Tooltip>
  );
}

function InviteRow({ invite }: { invite: Invite }) {
  const remove = useDeleteInvite();
  return (
    <TableRow>
      <TableCell className="font-medium">{invite.email}</TableCell>
      <TableCell className="max-w-72 truncate text-muted-foreground">
        {invite.note ?? ""}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {relativeTime(invite.created_at)}
      </TableCell>
      <TableCell className="w-20">
        <div className="flex items-center justify-end gap-1">
          <CopyLinkButton email={invite.email} />
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Revoke invite for ${invite.email}`}
                  disabled={remove.isPending}
                  onClick={() =>
                    remove.mutate(invite.email, {
                      onError: (error) =>
                        toast.error("Could not revoke invite", {
                          description: error.message,
                        }),
                    })
                  }
                />
              }
            >
              <Trash2Icon className="text-destructive" />
            </TooltipTrigger>
            <TooltipContent>Revoke invite</TooltipContent>
          </Tooltip>
        </div>
      </TableCell>
    </TableRow>
  );
}

function InviteForm() {
  const create = useCreateInvite();
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");

  const canSubmit = email.trim().includes("@") && !create.isPending;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    create.mutate(
      { email: email.trim().toLowerCase(), note: note.trim() || null },
      {
        onSuccess: (invite) => {
          toast.success(`${invite.email} invited`, {
            description:
              "Their first Google sign-in with this email creates the account.",
          });
          setEmail("");
          setNote("");
        },
        onError: (error) =>
          toast.error("Could not invite", {
            description:
              error instanceof ApiError && error.status === 409
                ? `${email.trim().toLowerCase()} is already invited.`
                : error.message,
          }),
      },
    );
  };

  return (
    <form
      onSubmit={submit}
      className="flex flex-wrap items-center gap-2 rounded-xl border bg-card p-3"
    >
      <Input
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="person@example.com"
        aria-label="Email to invite"
        className="w-64"
      />
      <Input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Note (optional, e.g. who they are)"
        aria-label="Invite note"
        className="min-w-48 flex-1"
      />
      <Button type="submit" disabled={!canSubmit}>
        <MailPlusIcon data-icon="inline-start" />
        Invite
      </Button>
    </form>
  );
}

export default function AdminInvitesPage() {
  const invites = useInvites();

  return (
    <div className="space-y-4">
      <InviteForm />
      {invites.isPending ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full rounded-lg" />
          ))}
        </div>
      ) : invites.isError ? (
        <QueryError
          error={invites.error}
          onRetry={() => void invites.refetch()}
        />
      ) : invites.data.length === 0 ? (
        <EmptyState
          icon={MailPlusIcon}
          title="No outstanding invites"
          description="Sign-ups are invite-only: add an email above and only that Google account can join. Emails already signed up no longer need their invite row."
        />
      ) : (
        <div className="rounded-xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Note</TableHead>
                <TableHead>Invited</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {invites.data.map((invite) => (
                <InviteRow key={invite.email} invite={invite} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
