"""Integrity regression thresholds (S5).

Two gates on the live integrity metrics: absolute FLOORS (a hard line — e.g. the
gate flag-rate must not exceed 50%) and a rolling-baseline regression check (a
metric must not worsen by more than ``regression_delta`` versus the recent
average). A warm-up rule applies: with no baseline yet, only the floors fire, so
the first runs don't always-pass or always-alert.
"""

from __future__ import annotations

from typing import Any

# metric -> (direction, bound). 'max' => higher is WORSE (a ceiling);
# 'min' => higher is BETTER (a floor).
DEFAULT_FLOORS: dict[str, tuple[str, float]] = {
    "gate_flag_rate": ("max", 0.50),
    "contested_rate": ("max", 0.50),
}

DEFAULT_REGRESSION_DELTA = 0.15


def evaluate(metrics: dict[str, float], *,
             floors: dict[str, tuple[str, float]] = DEFAULT_FLOORS,
             baseline: dict[str, float] | None = None,
             regression_delta: float = DEFAULT_REGRESSION_DELTA,
             ) -> list[dict[str, Any]]:
    """Return the list of threshold violations (empty => healthy)."""
    out: list[dict[str, Any]] = []
    for name, (direction, bound) in floors.items():
        v = metrics.get(name)
        if v is None:
            continue
        if direction == "max" and v > bound:
            out.append({"metric": name, "type": "floor", "value": round(v, 4),
                        "bound": bound})
        elif direction == "min" and v < bound:
            out.append({"metric": name, "type": "floor", "value": round(v, 4),
                        "bound": bound})
    # rolling-baseline regression (warm-up: skipped when no baseline exists)
    if baseline:
        for name, (direction, _bound) in floors.items():
            v, b = metrics.get(name), baseline.get(name)
            if v is None or b is None:
                continue
            worsened = (v - b) if direction == "max" else (b - v)
            if worsened > regression_delta:
                out.append({"metric": name, "type": "regression",
                            "value": round(v, 4), "baseline": round(b, 4),
                            "delta": round(worsened, 4)})
    return out
