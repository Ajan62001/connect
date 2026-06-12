"use client";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { DossierScope } from "@/lib/api";

/**
 * Mine | Shared | All dossier-list tabs (design §6 — the community feed
 * lives on the existing lists). The scope is sent to the backend as
 * `?scope=` AND applied client-side over each page (matchesDossierScope),
 * so the tabs are correct against both a scope-aware backend and one that
 * still ignores the param.
 */
const SCOPE_TABS: { value: DossierScope; label: string }[] = [
  { value: "mine", label: "Mine" },
  { value: "shared", label: "Shared" },
  { value: "all", label: "All" },
];

export function ScopeTabs({
  value,
  onChange,
}: {
  value: DossierScope;
  onChange: (scope: DossierScope) => void;
}) {
  return (
    <Tabs
      value={value}
      onValueChange={(next) => {
        if (next === "mine" || next === "shared" || next === "all") {
          onChange(next);
        }
      }}
    >
      <TabsList>
        {SCOPE_TABS.map((tab) => (
          <TabsTrigger key={tab.value} value={tab.value}>
            {tab.label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}
