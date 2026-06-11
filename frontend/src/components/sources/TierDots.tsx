import { cn } from "@/lib/utils";
import type { CredibilityTier } from "@/lib/api";

export const TIER_LABELS: Record<CredibilityTier, string> = {
  1: "Tier 1 — Official",
  2: "Tier 2 — Established press",
  3: "Tier 3 — Other media",
  4: "Tier 4 — Low trust",
};

/** Four dots; more filled = more credible (tier 1 fills all four). */
export function TierDots({ tier }: { tier: CredibilityTier }) {
  const filled = 5 - tier;
  return (
    <span
      className="inline-flex items-center gap-1"
      title={TIER_LABELS[tier]}
      aria-label={TIER_LABELS[tier]}
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <span
          key={i}
          aria-hidden
          className={cn(
            "size-1.5 rounded-full",
            i < filled ? "bg-foreground" : "bg-muted-foreground/25",
          )}
        />
      ))}
    </span>
  );
}
