"""Record integrity signals (S5).

``record`` writes one ``integrity_event`` row and, if a tracer is supplied,
mirrors a rate to Langfuse as a score. STRICTLY BEST-EFFORT: a failed write or
trace must never break the analysis/publish path it is observing, so every error
is swallowed. Mirrors the autocommit single-INSERT discipline of
``spend.record_call``.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from connect.integrity.signals import IntegritySignal
from connect.storage.pg import utc_now

log = logging.getLogger(__name__)


async def record(conn: psycopg.AsyncConnection, signal: IntegritySignal, *,
                 tracer: Any | None = None, user_id: int | None = None) -> None:
    """Append one integrity_event (best-effort) and optionally emit a tracer
    score for its rate."""
    try:
        await conn.execute(
            "INSERT INTO integrity_event (kind, surface, subject_id, numerator,"
            " denominator, value, user_id, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (signal.kind, signal.surface, signal.subject_id, signal.numerator,
             signal.denominator, signal.value, user_id, utc_now()))
    except Exception:  # noqa: BLE001 — observability never breaks the caller
        log.debug("integrity: failed to record %s/%s", signal.kind,
                  signal.surface, exc_info=True)
        return
    rate = signal.rate()
    if tracer is not None and rate is not None:
        try:
            tracer.score(name=f"integrity.{signal.kind}", value=rate,
                         metadata={"surface": signal.surface,
                                   "subject_id": signal.subject_id})
        except Exception:  # noqa: BLE001 — a tracer fault never breaks the caller
            log.debug("integrity: tracer.score failed", exc_info=True)


async def record_gate(conn: psycopg.AsyncConnection, gate: dict[str, Any], *,
                      surface: str, subject_id: int | None = None,
                      user_id: int | None = None,
                      tracer: Any | None = None) -> None:
    """Emit the integrity signals carried by a GateReport dump: the entailment-
    failure rate (flagged / checked) and the contested-source count."""
    if not gate:
        return
    checked = int(gate.get("checked") or 0)
    flagged = len(gate.get("flagged") or [])
    await record(conn, IntegritySignal(
        kind="gate", surface=surface, subject_id=subject_id,
        numerator=flagged, denominator=checked,
        value=gate.get("verdict")), tracer=tracer, user_id=user_id)
    contested = len(gate.get("contested") or [])
    if contested:
        await record(conn, IntegritySignal(
            kind="contested", surface=surface, subject_id=subject_id,
            numerator=contested, denominator=max(checked, 1)),
            tracer=tracer, user_id=user_id)
