"""DEAD REFERENCE — the v0.1 SQLite migration chain (v1-v10, closed).

Moved verbatim from connect/storage/migrations.py at port phase P2;
consumed ONLY by the one-shot ETL (P4), which refuses any source database
not at version 9/10. The live (PostgreSQL) lineage is
connect/storage/migrations.py.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from connect.tools.legacy_sqlite import schema
from connect.tools.legacy_sqlite.schema import (
    BRIEF_ITEM_INDEX_DDL,
    BRIEF_ITEM_TABLE_DDL,
    CLAIM_EMBEDDING_DDL,
    DOCUMENT_ENRICHMENT_DDL,
    DOCUMENT_FTS_TRIGGER_DDL,
    DOCUMENT_INDEX_DDL,
    DOCUMENT_LINK_DDL,
    DOCUMENT_TABLE_DDL,
    DOSSIER_SECTION_TABLE_DDL,
    DOSSIER_TABLE_DDL,
    EVENT_EMBEDDING_DDL,
    FINDING_DDL,
    FINDING_EVIDENCE_DDL,
    JOB_TABLE_DDL,
    POSITION_SHIFT_DDL,
    QUESTION_DDL,
    SCHEMA_VERSION,
    SOURCE_TABLE_DDL,
    STATEMENT_DDL,
    StorageError,
    StorageVersionError,
    VIEW_SUMMARY_DDL,
)


# ==============================================================================
# Legacy SQLite chain (v0.1 lineage, closed at v10) — TRANSITIONAL until P2/P5.
# ==============================================================================


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
    rowids are preserved by the copy. The copy-back lists the v3-era
    columns explicitly: the current DDL also carries the v10
    cancel_requested column, which takes its default here.
    """
    for ddl in DOCUMENT_ENRICHMENT_DDL:
        conn.execute(ddl)
    backup = "_mig_job"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM job")
    conn.execute("DROP TABLE job")
    conn.execute(JOB_TABLE_DDL)
    conn.execute(
        "INSERT INTO job (id, kind, payload, dossier_id, status, attempts,"
        " error, created_at, started_at, finished_at)"
        " SELECT id, kind, payload, dossier_id, status, attempts, error,"
        f" created_at, started_at, finished_at FROM {backup}")
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


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """v6 (Phase 2: events/threads/brief): event_embedding table (event
    centroid vectors for clustering) + brief_item.payload column
    (denormalized display fields frozen at brief generation).

    brief_item is rebuilt with the EXACT fresh-create DDL string (the
    v2 -> v3 pattern) so migrated and fresh DBs declare byte-identical
    tables; the copy-back lists the v5 columns explicitly — the new payload
    column takes its DDL default. The brief_item index (dropped with the
    table) is recreated after the copy. apply() runs with foreign_keys OFF.
    """
    for ddl in EVENT_EMBEDDING_DDL:
        conn.execute(ddl)
    backup = "_mig_brief_item"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM brief_item")
    conn.execute("DROP TABLE brief_item")
    conn.execute(BRIEF_ITEM_TABLE_DDL)
    conn.execute(
        "INSERT INTO brief_item (id, brief_id, section, rank, object_type,"
        " object_id, reason_json, seen)"
        " SELECT id, brief_id, section, rank, object_type, object_id,"
        f" reason_json, seen FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")
    for ddl in BRIEF_ITEM_INDEX_DDL:
        conn.execute(ddl)


def _migrate_6_to_7(conn: sqlite3.Connection) -> None:
    """v7 (Phase 3: verification slice): claim_embedding table (claim text
    vectors for reconciliation). Additive only — every other Phase 3 table
    (dossier, dossier_section, evidence, verdict_history, contradiction,
    claim_sighting) already shipped in schema v1.
    """
    for ddl in CLAIM_EMBEDDING_DDL:
        conn.execute(ddl)


def _migrate_7_to_8(conn: sqlite3.Connection) -> None:
    """v8 (investigation mode): question/finding/finding_evidence tables +
    three CHECK-vocabulary rebuilds.

    - dossier gains kind/parent_question_id/budget_usd columns and the
      topic/entity/story input types + investigation stages — rebuilt with
      the EXACT fresh-create DDL; the copy-back lists the v7 columns
      explicitly so the new columns take their DDL defaults
      (kind='analysis', parent_question_id NULL, budget_usd NULL).
    - dossier_section's stage vocabulary grows — rebuilt with SELECT *
      (column set unchanged).
    - job's kind vocabulary grows — rebuilt with an explicit column list
      (the current DDL also carries the v10 cancel_requested column, which
      takes its default here).
    Same copy-out -> drop -> recreate -> copy-back pattern as v2 -> v3;
    apply() runs with foreign_keys OFF, rowids survive the copies. The new
    tables are created FIRST so the rebuilt dossier's FK on question(id)
    resolves by name. Indexes dropped with a table are recreated after the
    copy-back.
    """
    for ddl in (*QUESTION_DDL, *FINDING_DDL, *FINDING_EVIDENCE_DDL):
        conn.execute(ddl)

    backup = "_mig_dossier"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM dossier")
    conn.execute("DROP TABLE dossier")
    conn.execute(DOSSIER_TABLE_DDL)
    conn.execute(
        "INSERT INTO dossier (id, title, input_text, input_type,"
        " input_document_id, status, current_stage, error, model_usage,"
        " created_at, started_at, finished_at)"
        " SELECT id, title, input_text, input_type, input_document_id,"
        " status, current_stage, error, model_usage, created_at,"
        f" started_at, finished_at FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")

    backup = "_mig_dossier_section"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM dossier_section")
    conn.execute("DROP TABLE dossier_section")
    conn.execute(DOSSIER_SECTION_TABLE_DDL)
    conn.execute(f"INSERT INTO dossier_section SELECT * FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")

    backup = "_mig_job"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM job")
    conn.execute("DROP TABLE job")
    conn.execute(JOB_TABLE_DDL)
    conn.execute(
        "INSERT INTO job (id, kind, payload, dossier_id, status, attempts,"
        " error, created_at, started_at, finished_at)"
        " SELECT id, kind, payload, dossier_id, status, attempts, error,"
        f" created_at, started_at, finished_at FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")


def _migrate_8_to_9(conn: sqlite3.Connection) -> None:
    """v9 (leader views & position tracking): statement / position_shift /
    view_summary tables + the brief_item.section vocabulary rebuild
    ('position_shift' joins BRIEF_SECTIONS).

    brief_item is rebuilt with the EXACT fresh-create DDL string (the
    v2 -> v3 pattern: copy out -> drop -> recreate -> copy back; column set
    unchanged, so SELECT *) — migrated and fresh DBs declare byte-identical
    tables. The brief_item index (dropped with the table) is recreated after
    the copy. apply() runs with foreign_keys OFF, rowids survive the copy.
    The new tables are plain creates (additive)."""
    for ddl in (*STATEMENT_DDL, *POSITION_SHIFT_DDL, *VIEW_SUMMARY_DDL):
        conn.execute(ddl)
    backup = "_mig_brief_item"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM brief_item")
    conn.execute("DROP TABLE brief_item")
    conn.execute(BRIEF_ITEM_TABLE_DDL)
    conn.execute(f"INSERT INTO brief_item SELECT * FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")
    for ddl in BRIEF_ITEM_INDEX_DDL:
        conn.execute(ddl)


def _migrate_9_to_10(conn: sqlite3.Connection) -> None:
    """v10 (v0.2 Phase A — queue/registry seam): job.cancel_requested, the
    durable cooperative-cancellation flag (CancelToken reads it at handler
    checkpoints; still-queued jobs are CAS-finished outright).

    job is rebuilt with the EXACT fresh-create DDL string (the v2 -> v3
    pattern: copy out -> drop -> recreate -> copy back); the copy-back
    lists the v9 columns explicitly so the new column takes its DDL
    default (0). apply() runs with foreign_keys OFF; rowids survive, so
    job_event children stay valid."""
    backup = "_mig_job"
    conn.execute(f"DROP TABLE IF EXISTS {backup}")
    conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM job")
    conn.execute("DROP TABLE job")
    conn.execute(JOB_TABLE_DDL)
    conn.execute(
        "INSERT INTO job (id, kind, payload, dossier_id, status, attempts,"
        " error, created_at, started_at, finished_at)"
        " SELECT id, kind, payload, dossier_id, status, attempts, error,"
        f" created_at, started_at, finished_at FROM {backup}")
    conn.execute(f"DROP TABLE {backup}")


# Registry: version N -> function taking N's schema to N+1's. Forward-only.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
    6: _migrate_6_to_7,
    7: _migrate_7_to_8,
    8: _migrate_8_to_9,
    9: _migrate_9_to_10,
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
