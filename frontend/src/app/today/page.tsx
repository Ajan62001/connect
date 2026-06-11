"use client";

import { BriefView } from "@/components/brief/BriefView";
import { useBriefToday } from "@/lib/queries";

/**
 * The daily brief — five SQL-rendered sections, generated lazily on the
 * first request of the day and stable until midnight.
 */
export default function TodayPage() {
  const brief = useBriefToday();
  return <BriefView query={brief} />;
}
