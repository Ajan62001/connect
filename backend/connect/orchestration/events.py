"""Progress events — durable job_event rows (seq = the SSE event id; the
rows are the replay log). The DAO commits each event immediately and
pg_notify's 'job_events' in the same transaction — api EventBus listeners
ping their SSE streams, and a lost NOTIFY costs only latency (streams
re-query on a fallback timer)."""

from __future__ import annotations

from typing import Any

import psycopg

from connect.storage import jobs as job_dao


async def emit(conn: psycopg.AsyncConnection, job_id: int, type_: str,
               data: dict[str, Any] | None = None) -> int:
    """Write one job_event row; returns its seq."""
    return await job_dao.insert_event(conn, job_id, type_, data)
