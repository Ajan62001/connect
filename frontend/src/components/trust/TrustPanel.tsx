"use client";

import type { CredibilityTier, TrustReport } from "@/lib/api";
import { TierDots, TIER_LABELS } from "@/components/sources/TierDots";
import { cn } from "@/lib/utils";

/**
 * The reader-facing "why trust this" panel (S6). Surfaces what the
 * editorial-integrity suite already knows about a generated item: the source
 * mix + credibility tiers (S1/S4), the verification-gate verdict (S2), any
 * corrections (S3), a balance score, and the AI-generated disclosure.
 */
export function TrustPanel({ report, compact = false }: { report: TrustReport; compact?: boolean }) {
  const conf = report.confidence ?? null;
  const flagged = report.gate_verdict === "flagged" || report.flagged_count > 0;
  return (
    <section className="rounded-lg border bg-muted/20 p-3 text-sm" aria-label="Why trust this">
      <header className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Why trust this
        </span>
        {conf !== null && (
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-xs font-medium",
              conf >= 0.7
                ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                : conf >= 0.5
                  ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
                  : "bg-red-500/15 text-red-700 dark:text-red-300",
            )}
            title="A coarse trust signal from the gate verdict and source corroboration"
          >
            {Math.round(conf * 100)}% confidence
          </span>
        )}
        {flagged && (
          <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-xs font-medium text-red-700 dark:text-red-300">
            ⚠ {report.flagged_count} unverified claim{report.flagged_count === 1 ? "" : "s"}
          </span>
        )}
      </header>

      {/* source mix + balance */}
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <TierMix mix={report.tier_mix} />
        <span>
          {report.independent_publishers} independent publisher
          {report.independent_publishers === 1 ? "" : "s"}
        </span>
        <span title="Source-diversity / one-sidedness score">
          Balance: <span className="font-medium text-foreground">{report.balance_label}</span>
          {report.one_sided ? " (single-source)" : ""}
        </span>
        {report.freshness && <span>Freshest source: {report.freshness.slice(0, 10)}</span>}
      </div>

      {/* contested / corrections warnings */}
      {report.contested.length > 0 && (
        <p className="mt-2 rounded-md bg-amber-500/10 px-2 py-1 text-xs text-amber-800 dark:text-amber-200">
          Built on contested evidence: {report.contested.length} claim
          {report.contested.length === 1 ? "" : "s"} the corpus marks disputed or refuted.
        </p>
      )}
      {report.corrections.length > 0 && (
        <p className="mt-1 rounded-md bg-red-500/10 px-2 py-1 text-xs text-red-800 dark:text-red-200">
          {report.corrections.length} correction{report.corrections.length === 1 ? "" : "s"} applied
          {report.corrections[0]?.detail ? ` — ${report.corrections[0].detail}` : ""}.
        </p>
      )}

      {/* per-source attribution with tiers + reliability */}
      {!compact && report.sources.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {report.sources.map((s, i) => {
            const tier = s.credibility_tier as CredibilityTier | null;
            return (
              <li key={i} className="flex items-center gap-2 text-xs">
                {tier && tier >= 1 && tier <= 4 ? (
                  <TierDots tier={tier} />
                ) : (
                  <span className="text-muted-foreground/60" title="Unregistered source">
                    ····
                  </span>
                )}
                {s.url ? (
                  <a href={s.url} target="_blank" rel="noopener noreferrer" className="underline">
                    {s.title || s.source_name || s.url}
                  </a>
                ) : (
                  <span>{s.title || s.source_name || "source"}</span>
                )}
                {s.source_name && (s.title || s.url) && (
                  <span className="text-muted-foreground"> — {s.source_name}</span>
                )}
                {tier && <span className="text-muted-foreground/70">{TIER_LABELS[tier]}</span>}
                {typeof s.reliability_score === "number" && (
                  <span
                    className="text-muted-foreground/70"
                    title="Data-driven reliability from this source's track record"
                  >
                    · rel {Math.round(s.reliability_score * 100)}%
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {report.ai_disclosure && (
        <p className="mt-2 text-[11px] italic text-muted-foreground">
          ⓘ AI-generated from the cited sources, human-reviewed before publishing.
        </p>
      )}
    </section>
  );
}

function TierMix({ mix }: { mix: Record<string, number> }) {
  const entries = Object.entries(mix).sort(([a], [b]) => Number(a) - Number(b));
  if (entries.length === 0) return <span>No tiered sources</span>;
  return (
    <span className="inline-flex items-center gap-2">
      {entries.map(([tier, n]) => {
        const t = Number(tier) as CredibilityTier;
        return (
          <span key={tier} className="inline-flex items-center gap-1" title={TIER_LABELS[t]}>
            <TierDots tier={t} />
            <span>×{n}</span>
          </span>
        );
      })}
    </span>
  );
}
