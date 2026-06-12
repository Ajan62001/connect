"use client";

import { useState } from "react";
import { CircleDollarSignIcon } from "lucide-react";

import { EmptyState } from "@/components/shared/EmptyState";
import { QueryError } from "@/components/shared/QueryError";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api";
import type { AdminSpendUser } from "@/lib/api";
import { formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useAdminSpend } from "@/lib/queries";

const DAY_CHOICES = [7, 14, 30] as const;

/**
 * Plain CSS usage bar: spend today against a cap. Capless rows scale
 * against `fallbackScale` (the global cap, or the largest spender) so the
 * bars stay comparable; >80% of a real cap goes hot.
 */
function UsageBar({
  value,
  cap,
  fallbackScale,
}: {
  value: number;
  cap: number | null;
  fallbackScale: number;
}) {
  const scale = cap !== null && cap > 0 ? cap : Math.max(fallbackScale, 0.01);
  const ratio = Math.min(1, value / scale);
  const hot = cap !== null && cap > 0 && value / cap > 0.8;
  return (
    <div
      className="h-2 w-full overflow-hidden rounded-full bg-muted"
      role="img"
      aria-label={`${formatUsd(value)}${cap !== null ? ` of ${formatUsd(cap)} cap` : ""}`}
    >
      <div
        className={cn(
          "h-full rounded-full transition-[width]",
          hot ? "bg-destructive" : "bg-primary",
        )}
        style={{ width: `${Math.max(ratio * 100, value > 0 ? 2 : 0)}%` }}
      />
    </div>
  );
}

function userLabel(user: AdminSpendUser): string {
  if (user.user_id === null) return "System jobs";
  return user.name ?? user.email ?? `member #${user.user_id}`;
}

function PerUserRow({
  user,
  fallbackScale,
}: {
  user: AdminSpendUser;
  fallbackScale: number;
}) {
  const system = user.user_id === null;
  return (
    <div className="grid grid-cols-[minmax(0,14rem)_1fr_auto] items-center gap-3">
      <div className="min-w-0 leading-tight">
        <p
          className={cn(
            "truncate text-sm",
            system ? "italic text-muted-foreground" : "font-medium",
          )}
        >
          {userLabel(user)}
        </p>
        {!system && user.email && user.name ? (
          <p className="truncate text-xs text-muted-foreground">{user.email}</p>
        ) : null}
      </div>
      <UsageBar
        value={user.today_usd}
        cap={user.cap_usd}
        fallbackScale={fallbackScale}
      />
      <p className="text-right text-xs tabular-nums text-muted-foreground">
        <span className="font-medium text-foreground">
          {formatUsd(user.today_usd)}
        </span>
        {user.cap_usd !== null ? ` / ${formatUsd(user.cap_usd)}` : " · no cap"}
        {user.investigation_cap_usd !== null
          ? ` (+${formatUsd(user.investigation_cap_usd)} inv.)`
          : ""}
      </p>
    </div>
  );
}

export default function AdminSpendPage() {
  const [days, setDays] = useState<number>(7);
  const spend = useAdminSpend(days);

  if (spend.isPending) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-32 w-full rounded-xl" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    );
  }
  if (spend.isError) {
    if (spend.error instanceof ApiError && spend.error.status === 404) {
      return (
        <EmptyState
          icon={CircleDollarSignIcon}
          title="System spend API not available yet"
          description="The backend hasn't shipped /api/admin/spend; the per-user dashboard lights up once it does."
        />
      );
    }
    return <QueryError error={spend.error} onRetry={() => void spend.refetch()} />;
  }

  const { global_cap_usd, global_today_usd, users, days: dayRows } = spend.data;
  const topSpend = Math.max(
    global_today_usd,
    ...users.map((user) => user.today_usd),
  );
  // Members first (biggest spender on top), system jobs at the bottom.
  const sorted = [...users].sort((a, b) => {
    if ((a.user_id === null) !== (b.user_id === null)) {
      return a.user_id === null ? 1 : -1;
    }
    return b.today_usd - a.today_usd;
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          Where the deployment&apos;s LLM budget goes — per user, against
          their effective caps.
        </p>
        <Tabs
          value={String(days)}
          onValueChange={(value) => setDays(Number(value))}
        >
          <TabsList>
            {DAY_CHOICES.map((choice) => (
              <TabsTrigger key={choice} value={String(choice)}>
                {choice}d
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Global envelope — today</CardTitle>
          <CardDescription>
            The deployment-wide backstop across all users and system jobs.
            Per-user caps live inside it; when it&apos;s spent, everything
            waits for tomorrow.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <UsageBar
            value={global_today_usd}
            cap={global_cap_usd}
            fallbackScale={topSpend}
          />
          <p className="text-sm tabular-nums">
            <span className="font-medium">{formatUsd(global_today_usd)}</span>
            <span className="text-muted-foreground">
              {global_cap_usd !== null
                ? ` of ${formatUsd(global_cap_usd)} global cap`
                : " spent today"}
            </span>
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Per-user — today</CardTitle>
          <CardDescription>
            Bars fill against each user&apos;s own daily cap (override or
            member default); investigation envelopes shown alongside.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {sorted.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No attributed spend yet today.
            </p>
          ) : (
            sorted.map((user) => (
              <PerUserRow
                key={user.user_id ?? "system"}
                user={user}
                fallbackScale={global_cap_usd ?? topSpend}
              />
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            System total — last {days} days
          </CardTitle>
        </CardHeader>
        <CardContent>
          {dayRows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No LLM calls in this window.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Day</TableHead>
                  <TableHead className="text-right">Calls</TableHead>
                  <TableHead className="text-right">Tokens in / out</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {dayRows.map((day) => (
                  <TableRow key={day.day}>
                    <TableCell className="font-mono text-xs">
                      {day.day}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {day.calls}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {day.input_tokens.toLocaleString()} /{" "}
                      {day.output_tokens.toLocaleString()}
                    </TableCell>
                    <TableCell className="text-right font-medium tabular-nums">
                      {formatUsd(day.cost_usd)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
