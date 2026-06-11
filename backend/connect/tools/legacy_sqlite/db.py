"""Connection factory + DB bootstrap.

One connection per process (all request handlers and background tasks are
async and run on the event loop thread; ``check_same_thread=False`` only
tolerates TestClient/threadpool edges). WAL + foreign_keys ON + busy_timeout
on every connection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from connect.tools.legacy_sqlite import migrations, schema


def utc_now() -> str:
    """ISO-8601 UTC timestamp with seconds precision — the one clock."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating parent dirs) with the standard pragmas. No DDL here."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> int:
    """Create the schema on a fresh DB, or migrate an existing one forward.

    Returns the resulting schema version. Refuses a DB newer than this code
    (StorageVersionError) — never silently corrupts.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        schema.create_all(conn)
        return schema.SCHEMA_VERSION
    db_version = migrations.read_version(conn)
    return migrations.apply(conn, db_version)
