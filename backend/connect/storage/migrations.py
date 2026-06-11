"""Schema versioning & migration — forward-only, log_buster pattern.

v0.2: the canonical lineage is PostgreSQL (``pg_migrate``/``pg_apply``,
baseline ``PG_SCHEMA_VERSION`` = 1, design v02-postgres-port.md §3):

- ``meta.schema_version`` is an integer; fresh DBs are stamped at the
  baseline by ``schema.create_all_pg``.
- Many api/worker replicas race at startup, so the whole reconcile runs in
  ONE transaction under ``pg_advisory_xact_lock`` (released automatically on
  commit/rollback/crash — no leak path); losers block, re-read the version
  AFTER acquiring the lock, and find nothing to do. PG DDL is transactional:
  a failed migration leaves no half-rebuilt tables.
- Migrations stay hand-rolled forward-only functions
  ``pg_migrate_N_to_N+1(conn)`` registered in ``PG_MIGRATIONS[N]`` — named
  CHECK constraints make vocabulary changes one ALTER, so the SQLite
  copy-out/drop/recreate rebuild class of migration is dead.
- A DB newer than the code is refused (StorageVersionError) — never silently
  corrupt with an older binary.

The closed v0.1 SQLite chain (v1-v10) moved to connect/tools/legacy_sqlite/
at port phase P2 (dead reference for the one-shot ETL only).
Migrations mirror schema.py's DDL constants where possible — schema.py stays
the single source of truth.
"""

from __future__ import annotations

from typing import Awaitable, Callable, TYPE_CHECKING

from connect.storage import schema
from connect.storage.schema import StorageError, StorageVersionError

if TYPE_CHECKING:
    import psycopg


# ==============================================================================
# PostgreSQL lineage (v0.2 canonical) — baseline 1, advisory-lock guarded.
# ==============================================================================

# One 64-bit advisory-lock key for schema work: ASCII "connect".
PG_MIGRATION_LOCK_KEY = 0x636F6E6E656374


async def pg_migrate_1_to_2(conn: "psycopg.AsyncConnection") -> None:
    """v2 — the runtime workstream (v02-runtime-deploy.md §2/§4): the job
    table becomes the SKIP LOCKED queue (priority/run_at claim order,
    claimed_by/heartbeat_at for the orphan sweep, max_attempts for
    requeues) and the shared runtime state arrives (fetch_domain politeness
    slots, robots_cache, beat_run nightly guards). New-table/index DDL
    comes verbatim from schema.py — the single DDL home."""
    await conn.execute(
        "ALTER TABLE job"
        " ADD COLUMN priority smallint NOT NULL DEFAULT 50,"
        " ADD COLUMN max_attempts integer NOT NULL DEFAULT 1,"
        " ADD COLUMN run_at timestamptz,"
        " ADD COLUMN claimed_by text,"
        " ADD COLUMN heartbeat_at timestamptz")
    await conn.execute("UPDATE job SET run_at = created_at")
    await conn.execute(
        "ALTER TABLE job ALTER COLUMN run_at SET NOT NULL,"
        " ALTER COLUMN run_at SET DEFAULT now()")
    for ddl in (
        *schema._PG_DDL_JOB_INDEXES,
        schema._PG_DDL_FETCH_DOMAIN,
        schema._PG_DDL_ROBOTS_CACHE,
        schema._PG_DDL_BEAT_RUN,
    ):
        await conn.execute(ddl)


# Registry: version N -> async function taking N's schema to N+1's.
# Forward-only.
PG_MIGRATIONS: dict[
    int, Callable[["psycopg.AsyncConnection"], Awaitable[None]]] = {
    1: pg_migrate_1_to_2,
}


def _scalar(row: object) -> object:
    """First column of a fetched row, tolerant of tuple/dict row factories."""
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]  # type: ignore[index]


async def pg_read_version(conn: psycopg.AsyncConnection) -> int:
    """The DB's recorded ``meta.schema_version``; missing row is an error."""
    import psycopg

    try:
        cur = await conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'")
        row = await cur.fetchone()
    except psycopg.Error as e:
        raise StorageError(f"cannot read schema_version: {e!r}") from e
    if row is None:
        raise StorageError("meta.schema_version missing — corrupt or foreign DB")
    return int(_scalar(row))  # type: ignore[arg-type]


async def pg_apply(conn: psycopg.AsyncConnection, db_version: int,
                   code_version: int = schema.PG_SCHEMA_VERSION) -> int:
    """Reconcile ``db_version`` forward to ``code_version``.

    Equal versions are a no-op; newer DBs are refused; older DBs walk the
    registry N -> N+1. The CALLER owns the surrounding transaction and the
    migration advisory lock (``pg_migrate``) — everything here is atomic
    with the version stamp because PG DDL is transactional.
    """
    if db_version == code_version:
        return db_version
    if db_version > code_version:
        raise StorageVersionError(
            f"DB schema_version {db_version} is newer than this code's "
            f"{code_version}; upgrade connect or use a different DB")
    v = db_version
    while v < code_version:
        mig = PG_MIGRATIONS.get(v)
        if mig is None:
            raise StorageError(
                f"no migration registered for schema {v} -> {v + 1}")
        await mig(conn)
        v += 1
    await conn.execute(
        "UPDATE meta SET value = %s WHERE key = 'schema_version'",
        (str(code_version),))
    return code_version


async def pg_migrate(conn: psycopg.AsyncConnection) -> int:
    """Create the schema on a fresh DB, or migrate an existing one forward.

    The single-flight schema entrypoint: one transaction, one
    ``pg_advisory_xact_lock`` (auto-released on commit/rollback/crash), the
    version re-read AFTER the lock so racing replicas serialize and losers
    no-op. Returns the resulting schema version; refuses newer DBs
    (StorageVersionError) — never silently corrupts.
    """
    import psycopg

    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)",
                               (PG_MIGRATION_LOCK_KEY,))
            cur = await conn.execute(
                "SELECT to_regclass('public.meta') AS meta_table")
            if _scalar(await cur.fetchone()) is None:
                await schema.create_all_pg(conn)
                return schema.PG_SCHEMA_VERSION
            db_version = await pg_read_version(conn)
            return await pg_apply(conn, db_version)
    except StorageError:
        raise
    except psycopg.Error as e:
        raise StorageError(
            f"PG schema create/migrate failed, rolled back: {e!r}") from e
