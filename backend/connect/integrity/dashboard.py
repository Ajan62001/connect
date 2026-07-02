"""Integrity dashboard aggregation (S5) — per-day rates over integrity_event,
plus the latest live-eval runs."""

from __future__ import annotations

from typing import Any

import psycopg


async def integrity_breakdown(conn: psycopg.AsyncConnection, *,
                              days: int = 14) -> dict[str, Any]:
    """Per-kind totals (rate + counts) and a per-day series over the window."""
    cur = await conn.execute(
        "SELECT kind,"
        " count(*) AS n,"
        " COALESCE(sum(numerator), 0) AS num,"
        " COALESCE(sum(denominator), 0) AS den"
        " FROM integrity_event"
        " WHERE created_at >= now() - make_interval(days => %s)"
        " GROUP BY kind ORDER BY kind", (days,))
    totals = []
    for r in await cur.fetchall():
        den = int(r["den"] or 0)
        totals.append({
            "kind": r["kind"], "events": int(r["n"]),
            "numerator": int(r["num"] or 0), "denominator": den,
            "rate": (int(r["num"] or 0) / den) if den else None})

    cur = await conn.execute(
        "SELECT kind, date_trunc('day', created_at)::date AS day,"
        " count(*) AS n, COALESCE(sum(numerator),0) AS num,"
        " COALESCE(sum(denominator),0) AS den"
        " FROM integrity_event"
        " WHERE created_at >= now() - make_interval(days => %s)"
        " GROUP BY kind, day ORDER BY day, kind", (days,))
    series = [{
        "kind": r["kind"], "day": str(r["day"]), "events": int(r["n"]),
        "rate": (int(r["num"] or 0) / int(r["den"])) if r["den"] else None}
        for r in await cur.fetchall()]

    # verdict distribution (categorical) is useful on its own
    cur = await conn.execute(
        "SELECT value, count(*) AS n FROM integrity_event"
        " WHERE kind = 'verdict' AND value IS NOT NULL"
        "   AND created_at >= now() - make_interval(days => %s)"
        " GROUP BY value ORDER BY value", (days,))
    verdicts = {r["value"]: int(r["n"]) for r in await cur.fetchall()}

    return {"days": days, "totals": totals, "series": series,
            "verdict_distribution": verdicts,
            "eval_runs": await latest_eval_runs(conn)}


async def latest_eval_runs(conn: psycopg.AsyncConnection, *,
                           limit: int = 10) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT id, started_at, finished_at, sample_size, metrics, regressions,"
        " status, trigger FROM integrity_eval_run"
        " ORDER BY started_at DESC LIMIT %s", (limit,))
    out = []
    for r in await cur.fetchall():
        out.append({
            "id": int(r["id"]), "started_at": str(r["started_at"]),
            "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
            "sample_size": int(r["sample_size"]),
            "metrics": dict(r["metrics"] or {}),
            "regressions": list(r["regressions"] or []),
            "status": r["status"], "trigger": r["trigger"]})
    return out
