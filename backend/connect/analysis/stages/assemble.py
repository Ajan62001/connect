"""Stage 2 — assembly (pure code this phase; the narrative dossier with
cited prose is a later phase). Collates the verdict_summary and the
per-stage summaries into the assemble section content."""

from __future__ import annotations

from typing import Any

from connect.analysis.schema import ClaimResult, VerdictSummary


def verdict_summary(claims: list[ClaimResult]) -> VerdictSummary:
    """Counts over CHECKABLE claims; a checkable claim that never got a
    verdict (budget abort) counts as unverified — honest accounting."""
    counts = {"supported": 0, "refuted": 0, "mixed": 0, "unverified": 0}
    for claim in claims:
        if not claim.checkable:
            continue
        counts[claim.verdict or "unverified"] += 1
    return VerdictSummary(**counts)


def run(claims: list[ClaimResult],
        stage_summaries: dict[str, str]) -> tuple[dict[str, Any], str]:
    """Returns (assemble section content, stage summary string)."""
    summary_model = verdict_summary(claims)
    parts = [f"{k} {v}" for k, v in summary_model.model_dump().items() if v]
    summary = "verdicts: " + (", ".join(parts) if parts else "none")
    content = {
        "verdict_summary": summary_model.model_dump(),
        "stage_summaries": dict(stage_summaries),
    }
    return content, summary
