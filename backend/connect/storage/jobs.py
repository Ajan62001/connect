"""Job + job_event DAO. job_event.seq doubles as the future SSE event id."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from connect.domain.models import Job
from connect.storage.db import utc_now


def _to_model(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        payload=json.loads(row["payload"] or "{}"),
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def create(conn: sqlite3.Connection, kind: str,
           payload: dict[str, Any] | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO job (kind, payload, status, created_at)"
            " VALUES (?,?, 'queued', ?)",
            (kind, json.dumps(payload or {}), utc_now()))
    return int(cur.lastrowid)  # type: ignore[arg-type]


def get(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    return _to_model(row) if row else None


def mark_running(conn: sqlite3.Connection, job_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE job SET status='running', started_at=?,"
            " attempts = attempts + 1 WHERE id = ?", (utc_now(), job_id))


def mark_finished(conn: sqlite3.Connection, job_id: int, status: str,
                  error: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE job SET status=?, error=?, finished_at=? WHERE id = ?",
            (status, error, utc_now(), job_id))


def reconcile_orphans(conn: sqlite3.Connection) -> int:
    """Startup: any 'running'/'queued' job from a previous process is dead."""
    with conn:
        cur = conn.execute(
            "UPDATE job SET status='failed', error='orphaned at startup',"
            " finished_at=? WHERE status IN ('running','queued')", (utc_now(),))
    return cur.rowcount


def insert_event(conn: sqlite3.Connection, job_id: int, type_: str,
                 data: dict[str, Any] | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO job_event (job_id, ts, type, data) VALUES (?,?,?,?)",
            (job_id, utc_now(), type_, json.dumps(data or {})))
    return int(cur.lastrowid)  # type: ignore[arg-type]
