"""Production integrity observability (S5).

The verification gate (S2), grounding core, and verdict engine all emit numeric
integrity signals; this package records them as an append-only `integrity_event`
stream (best-effort, like the spend ledger) and aggregates them into per-day
rates for an admin dashboard. A scheduled live-eval gate (evals/) then runs the
offline harness over sampled real traffic with regression thresholds.
"""

from connect.integrity.signals import IntegritySignal, KINDS, SURFACES
from connect.integrity import observe

__all__ = ["IntegritySignal", "KINDS", "SURFACES", "observe"]
