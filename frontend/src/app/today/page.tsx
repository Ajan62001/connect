import { SunriseIcon } from "lucide-react";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/shared/PageHeader";

export default function TodayPage() {
  return (
    <>
      <PageHeader
        title="Today"
        description="Your morning brief: watch developments, story threads that moved, new contradictions."
      />
      <EmptyState
        icon={SunriseIcon}
        title="The Today brief arrives in Phase 2"
        description="Once events and story threads land, this page becomes a daily, SQL-rendered brief of what changed in your corpus — no LLM required to read it."
      />
    </>
  );
}
