"""Schema versioning & migration — forward-only, log_buster pattern.

- ``meta.schema_version`` is an integer, starts at 1 (schema.py).
- Migrations are forward-only functions ``migrate_N_to_N+1(conn)`` registered
  in ``MIGRATIONS[N]``; applied in sequence inside one transaction.
- A DB newer than the code is refused (StorageVersionError) — never silently
  corrupt with an older binary.

Migrations mirror schema.py's DDL constants where possible (IF NOT EXISTS
DDL is identical on the fresh-create and migrate paths) — schema.py stays
the single source of truth.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from connect.storage.schema import (
    DOCUMENT_ENRICHMENT_DDL,
    DOCUMENT_FTS_TRIGGER_DDL,
    DOCUMENT_INDEX_DDL,
    DOCUMENT_LINK_DDL,
    DOCUMENT_TABLE_DDL,
    JOB_TABLE_DDL,
    SCHEMA_VERSION,
    SOURCE_TABLE_DDL,
    StorageError,
    StorageVersionError,
)


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """v2: document_link table + indexes (Phase 0.5 link extraction).

    Additive only — no existing table changes. edge.relation is free TEXT in
    v1 already, so the new 'links_to' relation needs no DDL change (the
    vocabulary lives in domain/enums.EDGE_RELATIONS, enforced in Python).
    """
    for ddl in DOCUMENT_LINK_DDL:
        conn.execute(ddl)


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """v3: source.type vocab += twitter/telegram; document.media_type vocab
    += tweet/telegram (the social-source adapters).

    SQLite cannot ALTER a CHECK constraint, so both tables are rebuilt with
    the EXACT fresh-create DDL strings from schema.py (copy out -> drop ->
    recreate -> copy back) — migrated and fresh DBs end up byte-identical in
    sqlite_master. Rowids survive (SELECT * carries the id column), so
    document_fts's external-content index stays valid; document's indexes
    and FTS triggers (dropped with the table) are recreated only AFTER the
    copy so FTS rows are not double-inserted. apply() has already turned
    foreign_keys OFF for the duration — otherwise DROP TABLE would fire
    ON DELETE actions (nulling document.source_id, cascading source_stats).
    """
    rebuilds: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("source", SOURCE_TABLE_DDL, ()),
        ("document", DOCUMENT_TABLE_DDL,
         (*DOCUMENT_INDEX_DDL, *DOCUMENT_FTS_TRIGGER_DDL)),
    )
    for table, table_ddl, extras in rebuilds:
        backup = f"_mig_{table}"
        conn.execute(f"DROP TABLE IF EXISTS {backup}")
        conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(table_ddl)
        conn.execute(f"INSERT INTO {table} SELECT * FROM {backup}")
        conn.execute(f"DROP TABLE {backup}")
        for ddl in extras:
            conn.execute(ddl)


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """v4: document_enrichment table (T1 results) + job.kind vocab gains
    'enrich_t1_sync' (the manual/sync sweep and the watch-hit fast-path).

    The job table is rebuilt with the EXACT fresh-create DDL string (same
    pattern as v2 -> v3: copy out -> drop -> recreate -> copy back) because
    SQLite cannot ALTER a CHECK constraint. job has no indexes or triggers;
    job_event's FK survives because apply() runs with foreign_keys OFF and
    rowids are preserved by the copy.
    """
    for ddl in DOCUMENT_ENRICHMENT_DDL:
        conn.execute(ddl)
    backup = "_mig_job"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM job")
    conn.execute("DROP TABLE job")
    conn.execute(JOB_TABLE_DDL)
    conn.execute(f"INSERT INTO job SELECT * FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """v5: document.media_type vocab += xlsx/docx (office extraction —
    gov spreadsheet annexes and Word files).

    SQLite cannot ALTER a CHECK constraint, so document is rebuilt with the
    EXACT fresh-create DDL string (the v2 -> v3 pattern: copy out -> drop ->
    recreate -> copy back) — migrated and fresh DBs end up byte-identical in
    sqlite_master. Rowids survive (SELECT * carries the id column), so
    document_fts's external-content index stays valid; document's indexes
    and FTS triggers (dropped with the table) are recreated only AFTER the
    copy so FTS rows are not double-inserted. apply() has already turned
    foreign_keys OFF for the duration.
    """
    backup = "_mig_document"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM document")
    conn.execute("DROP TABLE document")
    conn.execute(DOCUMENT_TABLE_DDL)
    conn.execute(f"INSERT INTO document SELECT * FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")
    for ddl in (*DOCUMENT_INDEX_DDL, *DOCUMENT_FTS_TRIGGER_DDL):
        conn.execute(ddl)


# Registry: version N -> function taking N's schema to N+1's. Forward-only.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
}


def read_version(conn: sqlite3.Connection) -> int:
    """The DB's recorded ``meta.schema_version``; missing row is an error."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error as e:
        raise StorageError(f"cannot read schema_version: {e!r}") from e
    if row is None:
        raise StorageError("meta.schema_version missing — corrupt or foreign DB")
    return int(row[0])


def apply(conn: sqlite3.Connection, db_version: int,
          code_version: int = SCHEMA_VERSION) -> int:
    """Reconcile ``db_version`` forward to ``code_version``.

    Equal versions are a no-op; newer DBs are refused; older DBs walk the
    registry N -> N+1 atomically (rolls back on any failure).

    Foreign keys are disabled for the duration (and restored after): table
    rebuilds must not fire ON DELETE actions, and the pragma is a silent
    no-op inside a transaction — so it is toggled out here, outside one.
    The explicit BEGIN pulls the DDL statements into the same transaction
    (pysqlite only auto-begins before DML), keeping the walk atomic.
    """
    if db_version == code_version:
        return db_version
    if db_version > code_version:
        raise StorageVersionError(
            f"DB schema_version {db_version} is newer than this code's "
            f"{code_version}; upgrade connect or use a different DB")
    fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if fk_was_on:
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute("BEGIN")
            v = db_version
            while v < code_version:
                mig = MIGRATIONS.get(v)
                if mig is None:
                    raise StorageError(
                        f"no migration registered for schema {v} -> {v + 1}")
                mig(conn)
                v += 1
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(code_version),))
            violations = conn.execute(
                "PRAGMA foreign_key_check(document)").fetchall()
            if violations:
                raise StorageError(
                    f"migration {db_version} -> {code_version} left "
                    f"{len(violations)} foreign-key violations on document; "
                    "rolled back")
    except StorageError:
        raise
    except sqlite3.Error as e:
        raise StorageError(
            f"migration {db_version} -> {code_version} failed, rolled back, "
            f"DB left at {db_version}: {e!r}") from e
    finally:
        if fk_was_on:
            conn.execute("PRAGMA foreign_keys=ON")
    return code_version
