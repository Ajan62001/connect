"""Integrity signal value object + vocabulary (S5)."""

from __future__ import annotations

from dataclasses import dataclass

# what is being measured
KINDS = (
    "gate",            # an editorial-gate run: numerator=flagged, denominator=checked
    "grounding",       # analysis grounding: numerator=cited, denominator=menu_size
    "off_menu",        # off-menu citation attempts (numerator) per run (denominator)
    "entailment_fail",  # gate entailment failures (numerator) per checked (denominator)
    "verdict",         # a claim verdict was set (value = the verdict)
    "contested",       # an item cited contested evidence (numerator = count)
)

# where it happened
SURFACES = ("content", "analysis", "story", "social")


@dataclass(frozen=True)
class IntegritySignal:
    """One measurement. Rate metrics carry numerator/denominator; categorical
    metrics carry a ``value`` (e.g. the verdict). ``subject_id`` is the loose id
    of the content_item / claim it concerns (no FK — signals outlive subjects)."""
    kind: str
    surface: str
    subject_id: int | None = None
    numerator: int | None = None
    denominator: int | None = None
    value: str | None = None

    def rate(self) -> float | None:
        if self.numerator is None or not self.denominator:
            return None
        return self.numerator / self.denominator
