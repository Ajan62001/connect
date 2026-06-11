"""Per-analysis USD budget (engine design §5) — the daily Governor still
applies on top; this cap stops ONE analysis from eating the day.

Policy (verify stage): when the projected cost of the next claim does not
fit, K degrades first (fewer evidence docs); when even the degraded shape
does not fit, the stage aborts with a partial result — never a silent
runaway, never a lost dossier.
"""

from __future__ import annotations


class AnalysisBudgetExceeded(RuntimeError):
    """This analysis' ledger + projection would exceed its USD cap."""


class AnalysisBudget:
    def __init__(self, cap_usd: float):
        self.cap_usd = cap_usd
        self.spent_usd = 0.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    def fits(self, projected_usd: float) -> bool:
        return self.spent_usd + projected_usd <= self.cap_usd

    def check(self, projected_usd: float = 0.0) -> None:
        if not self.fits(projected_usd):
            raise AnalysisBudgetExceeded(
                f"analysis budget exceeded: spent ${self.spent_usd:.4f} + "
                f"projected ${projected_usd:.4f} > ${self.cap_usd:.2f}")

    def add(self, cost_usd: float) -> None:
        self.spent_usd += cost_usd
