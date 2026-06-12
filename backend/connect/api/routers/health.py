"""/api/health (open — compose healthchecks) and /api/metrics
(admin-only Prometheus text), per runtime design §8.

``GET /api/health`` stays cheap: process up, schema version, vector
backend. ``?deep=1`` adds operational state — queue depth by kind/status,
oldest-queued age, beat liveness (beat_run freshness), last poll per
enabled source, blob-dir writability, spend today vs the global budget.
NO secrets either way: the DSN is password-redacted, keys never appear.
"""

from __future__ import annotations

import os

import psycopg
from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from connect.api.deps import get_container, get_db, require_admin
from connect.domain.models import (
    BudgetHealth,
    Health,
    HealthDeep,
    QueueDepth,
    SourcePollHealth,
)
from connect.llm import spend as spend_mod
from connect.llm.spend import INVESTIGATION_PURPOSES
from connect.orchestration.container import Container
from connect.storage import app_settings as app_settings_dao
from connect.storage.pg import redact_dsn

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health,
            response_model_exclude_none=True)
async def health(deep: bool = Query(default=False),
                 container: Container = Depends(get_container)) -> Health:
    base = dict(
        ok=True,
        schema_version=container.schema_version,
        # the DSN, password-redacted: health is unauthenticated and proxied
        db_path=redact_dsn(container.settings.database_url),
        vector_backend=container.vector_backend,
    )
    if not deep:
        return Health(**base)  # type: ignore[arg-type]
    async with container.pool.connection() as conn:
        return Health(**base, deep=await _deep(conn, container))  # type: ignore[arg-type]


async def _deep(conn: psycopg.AsyncConnection,
                container: Container) -> HealthDeep:
    cur = await conn.execute(
        "SELECT kind, status, count(*) AS count FROM job"
        " WHERE status IN ('queued', 'running')"
        " GROUP BY kind, status ORDER BY kind, status")
    queue = [QueueDepth(**r) for r in await cur.fetchall()]

    cur = await conn.execute(
        "SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))::float8"
        " AS age FROM job WHERE status = 'queued'")
    row = await cur.fetchone()
    oldest = float(row["age"]) if row and row["age"] is not None else None

    cur = await conn.execute(
        "SELECT task, last_run_at FROM beat_run ORDER BY task")
    beat_runs = {r["task"]: r["last_run_at"] for r in await cur.fetchall()}

    cur = await conn.execute(
        "SELECT id, name, last_polled_at, last_poll_status FROM source"
        " WHERE enabled ORDER BY id")
    sources = [SourcePollHealth(**r) for r in await cur.fetchall()]

    effective = await app_settings_dao.effective_budgets(
        conn, container.settings)
    budget = BudgetHealth(
        global_cap_usd=effective["global_daily_budget_usd"],
        today_usd=await spend_mod.spent_today(conn),
        general_today_usd=await spend_mod.spent_today(
            conn, exclude_purposes=INVESTIGATION_PURPOSES),
        investigation_today_usd=await spend_mod.spent_today(
            conn, purposes=INVESTIGATION_PURPOSES),
    )

    # BlobStore mkdirs lazily on the first put — when the dir doesn't
    # exist yet, the deepest existing ancestor's writability is the truth
    blob_path = container.blobs.root
    while not blob_path.exists() and blob_path.parent != blob_path:
        blob_path = blob_path.parent
    return HealthDeep(
        pg_ok=True,  # we are answering over a pooled PG connection
        queue=queue,
        queue_oldest_seconds=oldest,
        beat_runs=beat_runs,
        sources=sources,
        blob_dir_writable=os.access(blob_path, os.W_OK),
        budget=budget,
    )


def _metric(name: str, value: float, labels: dict[str, str] | None = None,
            ) -> str:
    if labels:
        inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{inner}}} {value}"
    return f"{name} {value}"


@router.get("/metrics", response_class=PlainTextResponse,
            dependencies=[Depends(require_admin)])
async def metrics(container: Container = Depends(get_container),
                  db: psycopg.AsyncConnection = Depends(get_db)) -> str:
    """Prometheus text format, hand-formatted (runtime design §8 —
    admin-only; scrapeable later without rework, human-readable now)."""
    lines: list[str] = []

    cur = await db.execute(
        "SELECT kind, status, count(*) AS count FROM job"
        " GROUP BY kind, status ORDER BY kind, status")
    for r in await cur.fetchall():
        lines.append(_metric("connect_jobs", int(r["count"]),
                             {"kind": r["kind"], "status": r["status"]}))

    cur = await db.execute(
        "SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))::float8"
        " AS age FROM job WHERE status = 'queued'")
    row = await cur.fetchone()
    lines.append(_metric(
        "connect_job_queue_oldest_seconds",
        round(float(row["age"]), 3)
        if row and row["age"] is not None else 0.0))

    lines.append(_metric(
        "connect_llm_spend_usd_today",
        round(await spend_mod.spent_today(db), 6), {"scope": "global"}))
    lines.append(_metric(
        "connect_llm_spend_usd_today",
        round(await spend_mod.spent_today(
            db, exclude_purposes=INVESTIGATION_PURPOSES), 6),
        {"scope": "general"}))
    lines.append(_metric(
        "connect_llm_spend_usd_today",
        round(await spend_mod.spent_today(
            db, purposes=INVESTIGATION_PURPOSES), 6),
        {"scope": "investigation"}))

    cur = await db.execute(
        "SELECT count(*) AS n,"
        " count(*) FILTER (WHERE disabled) AS disabled FROM app_user")
    row = await cur.fetchone()
    lines.append(_metric("connect_users_total", int(row["n"])))
    lines.append(_metric("connect_users_disabled", int(row["disabled"])))

    cur = await db.execute(
        "SELECT count(*) AS n FROM user_session WHERE expires_at > now()")
    lines.append(_metric("connect_sessions_active",
                         int((await cur.fetchone())["n"])))

    return "\n".join(lines) + "\n"
