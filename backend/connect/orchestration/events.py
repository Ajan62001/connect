"""Progress events — job_event rows now; SSE fan-out is a stub this phase
(job_event.seq is already the future SSE event id, so reconnect/replay
semantics are baked into the rows)."""

from __future__ import annotations

import sqlite3
from typing import Any

from connect.storage import jobs as job_dao


def emit(conn: sqlite3.Connection, job_id: int, type_: str,
         data: dict[str, Any] | None = None) -> int:
    """Write one job_event row; returns its seq."""
    return job_dao.insert_event(conn, job_id, type_, data)
