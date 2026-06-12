"use client";

import { useState } from "react";
import { UsersIcon } from "lucide-react";
import { toast } from "sonner";

import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError } from "@/lib/api";
import type { AdminUser, AdminUserUpdate, Role } from "@/lib/api";
import { formatUsd, relativeTime } from "@/lib/format";
import {
  useAdminSettings,
  useAdminUsers,
  useMe,
  useUpdateAdminUser,
} from "@/lib/queries";

/** value -> label map for the role Select (labels equal the wire values). */
const ROLE_ITEMS: Record<Role, string> = { member: "member", admin: "admin" };

function initialsOf(user: AdminUser): string {
  const source = user.name?.trim() || user.email;
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

function UserCell({ user }: { user: AdminUser }) {
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      {user.avatar_url ? (
        // eslint-disable-next-line @next/next/no-img-element -- tiny avatar; Google hosts need no-referrer
        <img
          src={user.avatar_url}
          alt=""
          referrerPolicy="no-referrer"
          className="size-7 shrink-0 rounded-full border"
        />
      ) : (
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-semibold text-primary">
          {initialsOf(user)}
        </span>
      )}
      <div className="min-w-0 leading-tight">
        <p className="truncate text-sm font-medium">
          {user.name ?? user.email}
        </p>
        <p className="truncate text-xs text-muted-foreground">{user.email}</p>
      </div>
    </div>
  );
}

/**
 * One budget-override cell: blank = inherit the member default (shown as
 * the placeholder), a number = per-user override. Commits on blur/Enter
 * only when the value actually changed; an explicit blank PATCHes null to
 * clear the override (design §4 precedence: override > setting > env).
 */
function BudgetOverrideInput({
  value,
  fallback,
  label,
  onCommit,
  disabled,
}: {
  value: number | null;
  fallback: number | null;
  label: string;
  onCommit: (value: number | null) => void;
  disabled: boolean;
}) {
  const canonical = value === null ? "" : String(value);
  const [draft, setDraft] = useState(canonical);
  // Re-sync when a refetch lands a new canonical value (render-time reset —
  // the React-blessed "derived state from props" pattern, no effect).
  const [lastCanonical, setLastCanonical] = useState(canonical);
  if (lastCanonical !== canonical) {
    setLastCanonical(canonical);
    setDraft(canonical);
  }

  const commit = () => {
    const trimmed = draft.trim();
    if (trimmed === canonical.trim()) return;
    if (trimmed === "") {
      onCommit(null);
      return;
    }
    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed) || parsed < 0) {
      setDraft(canonical); // reject silently — revert to the saved value
      return;
    }
    onCommit(parsed);
  };

  return (
    <div className="flex items-center gap-1">
      <span className="text-xs text-muted-foreground">$</span>
      <Input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") setDraft(canonical);
        }}
        placeholder={fallback !== null ? String(fallback) : "default"}
        inputMode="decimal"
        disabled={disabled}
        aria-label={label}
        className="h-7 w-20 text-right text-xs tabular-nums"
      />
    </div>
  );
}

function UserRow({
  user,
  isSelf,
  defaults,
}: {
  user: AdminUser;
  isSelf: boolean;
  defaults: { general: number | null; investigation: number | null };
}) {
  const update = useUpdateAdminUser();

  const patch = (payload: AdminUserUpdate) =>
    update.mutate(
      { id: user.id, payload },
      {
        onError: (error) =>
          toast.error(`Could not update ${user.email}`, {
            description: error.message,
          }),
      },
    );

  return (
    <TableRow className={user.disabled ? "opacity-60" : undefined}>
      <TableCell className="max-w-64">
        <UserCell user={user} />
      </TableCell>
      <TableCell>
        <Select
          items={ROLE_ITEMS}
          value={user.role}
          onValueChange={(value) => {
            if (value === "admin" || value === "member") {
              patch({ role: value });
            }
          }}
          disabled={isSelf || update.isPending}
        >
          <SelectTrigger
            size="sm"
            className="w-28"
            title={isSelf ? "You cannot change your own role" : undefined}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="member">member</SelectItem>
            <SelectItem value="admin">admin</SelectItem>
          </SelectContent>
        </Select>
      </TableCell>
      <TableCell>
        <div
          className="flex items-center gap-2"
          title={isSelf ? "You cannot disable yourself" : undefined}
        >
          <Switch
            size="sm"
            checked={!user.disabled}
            disabled={isSelf || update.isPending}
            aria-label={`${user.email} active`}
            onCheckedChange={(checked) => patch({ disabled: !checked })}
          />
          <span className="text-xs text-muted-foreground">
            {user.disabled ? "disabled" : "active"}
          </span>
        </div>
      </TableCell>
      <TableCell>
        <BudgetOverrideInput
          value={user.daily_budget_usd}
          fallback={user.role === "admin" ? null : defaults.general}
          label={`${user.email} daily budget override`}
          disabled={update.isPending}
          onCommit={(value) => patch({ daily_budget_usd: value })}
        />
      </TableCell>
      <TableCell>
        <BudgetOverrideInput
          value={user.investigation_daily_budget_usd}
          fallback={user.role === "admin" ? null : defaults.investigation}
          label={`${user.email} investigation budget override`}
          disabled={update.isPending}
          onCommit={(value) => patch({ investigation_daily_budget_usd: value })}
        />
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {user.last_login_at ? relativeTime(user.last_login_at) : "never"}
      </TableCell>
    </TableRow>
  );
}

export default function AdminUsersPage() {
  const me = useMe();
  const users = useAdminUsers();
  const settings = useAdminSettings();

  if (users.isPending) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full rounded-lg" />
        ))}
      </div>
    );
  }
  if (users.isError) {
    if (users.error instanceof ApiError && users.error.status === 404) {
      return (
        <EmptyState
          icon={UsersIcon}
          title="User management API not available yet"
          description="The backend hasn't shipped /api/admin/users; this table lights up once it does."
        />
      );
    }
    return <QueryError error={users.error} onRetry={() => void users.refetch()} />;
  }
  if (users.data.length === 0) {
    return (
      <EmptyState
        icon={UsersIcon}
        title="No users yet"
        description="Invite someone — the first Google sign-in for an invited email creates the account."
      />
    );
  }

  const defaults = {
    general: settings.data?.member_daily_budget_usd ?? null,
    investigation: settings.data?.member_investigation_daily_budget_usd ?? null,
  };

  return (
    <div className="space-y-3">
      <div className="rounded-xl border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>User</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Daily budget</TableHead>
              <TableHead>Investigation budget</TableHead>
              <TableHead>Last login</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {users.data.map((user) => (
              <UserRow
                key={user.id}
                user={user}
                isSelf={user.id === me.data?.id}
                defaults={defaults}
              />
            ))}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground">
        Budgets: blank = the member defaults
        {defaults.general !== null
          ? ` (${formatUsd(defaults.general)} general`
          : ""}
        {defaults.investigation !== null
          ? `, ${formatUsd(defaults.investigation)} investigation/day)`
          : defaults.general !== null
            ? ")"
            : ""}
        ; a value here overrides them for that user. Admins fall back to the
        deployment&apos;s admin caps instead. Disabling a user rejects their
        sessions immediately; their shared work stays in the knowledge base.
      </p>
    </div>
  );
}
