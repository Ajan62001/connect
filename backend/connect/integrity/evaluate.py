"""The live integrity-eval gate (S5).

A scheduled run snapshots the recent integrity_event stream into a handful of
metrics (the gate flag-rate, contested-source rate, …), checks them against
absolute floors + a rolling baseline of prior runs, persists an
``integrity_eval_run`` ledger row, and logs an alert on any regression. It needs
no LLM and no gold data — it watches what production ACTUALLY did — so it runs on
any deploy, keyed or not.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from connect.integrity import thresholds
from connect.storage.pg import Jsonb, utc_now

log = logging.getLogger(__name__)

# how many prior runs form the rolling baseline
BASELINE_RUNS = 5


async def _metrics(conn: psycopg.AsyncConnection, days: int) -> dict[str, float]:
    cur = await conn.execute(
        "SELECT"
        " COALESCE(sum(numerator) FILTER (WHERE kind='gate'),0) AS gate_flag,"
        " COALESCE(sum(denominator) FILTER (WHERE kind='gate'),0) AS gate_chk,"
        " COALESCE(sum(numerator) FILTER (WHERE kind='contested'),0) AS cont_n,"
        " COALESCE(sum(denominator) FILTER (WHERE kind='contested'),0) AS cont_d,"
        " count(*) FILTER (WHERE kind='gate') AS gate_runs"
        " FROM integrity_event"
        " WHERE created_at >= now() - make_interval(days => %s)", (days,))
    r = await cur.fetchone()
    gate_chk = float(r["gate_chk"] or 0)
    cont_d = float(r["cont_d"] or 0)
    m: dict[str, float] = {"sample_size": float(r["gate_runs"] or 0)}
    if gate_chk:
        m["gate_flag_rate"] = float(r["gate_flag"] or 0) / gate_chk
    if cont_d:
        m["contested_rate"] = float(r["cont_n"] or 0) / cont_d
    return m


async def _baseline(conn: psycopg.AsyncConnection) -> dict[str, float]:
    cur = await conn.execute(
        "SELECT metrics FROM integrity_eval_run WHERE status <> 'error'"
        " ORDER BY started_at DESC LIMIT %s", (BASELINE_RUNS,))
    rows = [dict(r["metrics"] or {}) for r in await cur.fetchall()]
    agg: dict[str, list[float]] = {}
    for m in rows:
        for k, v in m.items():
            if isinstance(v, (int, float)):
                agg.setdefault(k, []).append(float(v))
    return {k: sum(vs) / len(vs) for k, vs in agg.items() if vs}


async def run_eval(conn: psycopg.AsyncConnection, *, days: int = 1,
                   trigger: str = "nightly") -> dict[str, Any]:
    """Compute live integrity metrics, check thresholds, persist a run row."""
    started = utc_now()
    metrics = await _metrics(conn, days)
    baseline = await _baseline(conn)
    regressions = thresholds.evaluate(metrics, baseline=baseline or None)
    status = "regressed" if regressions else "ok"
    cur = await conn.execute(
        "INSERT INTO integrity_eval_run (started_at, finished_at, sample_size,"
        " metrics, regressions, status, trigger)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (started, utc_now(), int(metrics.get("sample_size", 0)),
         Jsonb(metrics), Jsonb(regressions), status, trigger))
    run_id = int((await cur.fetchone())["id"])
    if regressions:
        log.warning("integrity eval %s REGRESSED: %s", run_id, regressions)
    return {"id": run_id, "status": status, "metrics": metrics,
            "regressions": regressions}
