"use client";

import { use } from "react";
import { CalendarOffIcon } from "lucide-react";

import { BriefView } from "@/components/brief/BriefView";
import { EmptyState } from "@/components/shared/EmptyState";
import { useBrief } from "@/lib/queries";

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** A past day's brief — same view as /today, keyed by 'YYYY-MM-DD'. */
export default function BriefDatePage({
  params,
}: {
  params: Promise<{ date: string }>;
}) {
  const { date } = use(params);
  const valid = DATE_RE.test(date);
  const brief = useBrief(date, valid);

  if (!valid) {
    return (
      <EmptyState
        icon={CalendarOffIcon}
        title="Invalid brief date"
        description={`“${date}” is not a YYYY-MM-DD date.`}
      />
    );
  }

  return <BriefView query={brief} />;
}
