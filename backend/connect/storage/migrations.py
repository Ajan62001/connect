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


async def pg_migrate_2_to_3(conn: "psycopg.AsyncConnection") -> None:
    """v3 — Phase C tenancy (v02-tenancy-auth.md §2 deferred tightening):
    the per-user surfaces become NOT NULL. Pre-tenancy NULL rows (single-
    user era / ETL'd data) are backfilled to the FIRST ADMIN — exactly the
    design §7 single-user migration rule; with no rows to backfill (fresh
    deployments) the ALTERs are trivially satisfiable. view_cursor's PK
    swaps to (user_id, surface, ref_id). A deployment that somehow has
    orphan rows but no admin fails loudly (SET NOT NULL) rather than
    guessing an owner."""
    cur = await conn.execute(
        "SELECT id FROM app_user WHERE role = 'admin' AND NOT disabled"
        " ORDER BY id LIMIT 1")
    row = await cur.fetchone()
    admin_id = None if row is None else _scalar(row)
    if admin_id is not None:
        await conn.execute(
            "UPDATE dossier SET owner_id = %s WHERE owner_id IS NULL",
            (admin_id,))
        for table in ("watch", "brief", "view_cursor"):
            await conn.execute(
                f"UPDATE {table} SET user_id = %s WHERE user_id IS NULL",
                (admin_id,))
    await conn.execute(
        "ALTER TABLE dossier ALTER COLUMN owner_id SET NOT NULL")
    await conn.execute(
        "ALTER TABLE watch ALTER COLUMN user_id SET NOT NULL")
    await conn.execute(
        "ALTER TABLE brief ALTER COLUMN user_id SET NOT NULL")
    await conn.execute(
        "ALTER TABLE view_cursor ALTER COLUMN user_id SET NOT NULL")
    await conn.execute(
        "ALTER TABLE view_cursor DROP CONSTRAINT view_cursor_pkey")
    await conn.execute(
        "ALTER TABLE view_cursor"
        " ADD PRIMARY KEY (user_id, surface, ref_id)")


async def pg_migrate_3_to_4(conn: "psycopg.AsyncConnection") -> None:
    """v4 — web_news source type: extend ck_source_type CHECK vocabulary.
    Named constraint (introduced in PG v1) makes this a non-rebuild ALTER."""
    await conn.execute(
        "ALTER TABLE source DROP CONSTRAINT ck_source_type")
    await conn.execute(
        "ALTER TABLE source ADD CONSTRAINT ck_source_type"
        f" CHECK (type IN {schema.E.sql_in(schema.E.SOURCE_TYPES)})")


async def pg_migrate_4_to_5(conn: "psycopg.AsyncConnection") -> None:
    """v5 — historical backfill: add the 'backfill' document origin and the
    'backfill_source' job kind. Both are named-CHECK vocabulary extensions
    (document.ck_document_origin, job.ck_job_kind) — non-rebuild ALTERs."""
    await conn.execute(
        "ALTER TABLE document DROP CONSTRAINT ck_document_origin")
    await conn.execute(
        "ALTER TABLE document ADD CONSTRAINT ck_document_origin"
        f" CHECK (origin IN {schema.E.sql_in(schema.E.DOCUMENT_ORIGINS)})")
    await conn.execute(
        "ALTER TABLE job DROP CONSTRAINT ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")


async def pg_migrate_5_to_6(conn: "psycopg.AsyncConnection") -> None:
    """v6 — user-authored findings board: a new owned/visibility ``post``
    table (CREATE TABLE from schema's DDL — app_user/document already exist)."""
    for ddl in (schema._PG_DDL_POST, *schema._PG_DDL_POST_INDEXES):
        await conn.execute(ddl)


async def pg_migrate_6_to_7(conn: "psycopg.AsyncConnection") -> None:
    """v7 — workspaces (saved lens over the shared corpus): the new
    ``workspace`` table, plus an optional ``workspace_id`` tag on post and
    watch (FK ON DELETE SET NULL — deleting a workspace untags, never drops,
    its findings/watches)."""
    for ddl in (schema._PG_DDL_WORKSPACE, *schema._PG_DDL_WORKSPACE_INDEXES):
        await conn.execute(ddl)
    for table in ("post", "watch"):
        await conn.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS workspace_id bigint"
            " REFERENCES workspace(id) ON DELETE SET NULL")


async def pg_migrate_7_to_8(conn: "psycopg.AsyncConnection") -> None:
    """v8 — persisted workspace-agent conversations: the new
    ``workspace_chat`` table (CREATE from schema's DDL — workspace/app_user
    already exist)."""
    for ddl in (schema._PG_DDL_WORKSPACE_CHAT,
                *schema._PG_DDL_WORKSPACE_CHAT_INDEXES):
        await conn.execute(ddl)


async def pg_migrate_8_to_9(conn: "psycopg.AsyncConnection") -> None:
    """v9 — per-workspace post-generation settings: an overrides jsonb on
    workspace (merged over the global default at use time)."""
    await conn.execute(
        "ALTER TABLE workspace ADD COLUMN IF NOT EXISTS post_settings jsonb"
        " NOT NULL DEFAULT '{}'::jsonb")


async def pg_migrate_9_to_10(conn: "psycopg.AsyncConnection") -> None:
    """v10 — social-post drafts the workspace agent generates (new
    ``social_draft`` table; workspace/app_user/document already exist)."""
    for ddl in (schema._PG_DDL_SOCIAL_DRAFT,
                *schema._PG_DDL_SOCIAL_DRAFT_INDEXES):
        await conn.execute(ddl)


async def pg_migrate_10_to_11(conn: "psycopg.AsyncConnection") -> None:
    """v11 — per-workspace knowledge base: tag documents with a workspace
    (a plain bigint column — stale ids after a workspace delete are inert
    since reads filter by a live workspace id)."""
    await conn.execute(
        "ALTER TABLE document ADD COLUMN IF NOT EXISTS workspace_id bigint")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_workspace"
        " ON document(workspace_id, fetched_at DESC)"
        " WHERE workspace_id IS NOT NULL")


async def pg_migrate_11_to_12(conn: "psycopg.AsyncConnection") -> None:
    """v12 — long-running workspace-agent tasks: the new ``workspace_task``
    table + the ``workspace_task`` job kind (extend the ck_job_kind CHECK)."""
    for ddl in (schema._PG_DDL_WORKSPACE_TASK,
                *schema._PG_DDL_WORKSPACE_TASK_INDEXES):
        await conn.execute(ddl)
    await conn.execute("ALTER TABLE job DROP CONSTRAINT ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")


async def pg_migrate_12_to_13(conn: "psycopg.AsyncConnection") -> None:
    """v13 — grounding gate for agent-authored findings: the ``post`` table
    gains a verbatim-verified quote (quote + char offsets into the cited
    document), NULL for human posts."""
    await conn.execute(
        "ALTER TABLE post ADD COLUMN IF NOT EXISTS quote text")
    await conn.execute(
        "ALTER TABLE post ADD COLUMN IF NOT EXISTS quote_start integer")
    await conn.execute(
        "ALTER TABLE post ADD COLUMN IF NOT EXISTS quote_end integer")


async def pg_migrate_13_to_14(conn: "psycopg.AsyncConnection") -> None:
    """v14 — retire the ``workspace_task`` table: deep workspace-agent runs are
    now async chat jobs that stream job_event and append to the chat
    transcript, so the bespoke task row (and its stuck-status failure modes)
    is gone. The ``workspace_task`` job kind is KEPT — it is the deep-run job.
    """
    await conn.execute("DROP TABLE IF EXISTS workspace_task")


async def pg_migrate_14_to_15(conn: "psycopg.AsyncConnection") -> None:
    """v15 — story mode: extend the dossier kind + input_type and job kind
    CHECK constraints for the new 'story' dossier/job and its
    'investigation'/'workspace' sources. Reuses the existing 'scope' and
    'synthesize' section stages, so no dossier_section change."""
    await conn.execute(
        "ALTER TABLE dossier DROP CONSTRAINT IF EXISTS ck_dossier_kind")
    await conn.execute(
        "ALTER TABLE dossier ADD CONSTRAINT ck_dossier_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.DOSSIER_KINDS)})")
    await conn.execute(
        "ALTER TABLE dossier DROP CONSTRAINT IF EXISTS ck_dossier_input_type")
    await conn.execute(
        "ALTER TABLE dossier ADD CONSTRAINT ck_dossier_input_type"
        " CHECK (input_type IN"
        f" {schema.E.sql_in(schema.E.DOSSIER_INPUT_TYPES)})")
    await conn.execute(
        "ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")


async def pg_migrate_15_to_16(conn: "psycopg.AsyncConnection") -> None:
    """v16 — social content pipeline: the `campaign` + `content_item` tables
    (the review queue) and the `content_generate` / `content_publish` job
    kinds. Fresh DDL only (no data migration); the job-kind CHECK is widened
    and the publish-dedup partial index is created."""
    for ddl in schema._PG_DDL_CAMPAIGN_DDL:
        await conn.execute(ddl)
    await conn.execute(
        "ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_publish_item"
        " ON job ((payload->>'item_id'))"
        " WHERE kind = 'content_publish' AND status IN ('queued','running')")


async def pg_migrate_16_to_17(conn: "psycopg.AsyncConnection") -> None:
    """v17 — Instagram Reels: the new `ig_reel` content format. Widen the
    content_item format CHECK to the current CONTENT_FORMATS (platform
    `instagram` is already allowed; reel videos ride in card_shas, so no new
    column). Pure constraint widening, no data migration."""
    await conn.execute(
        "ALTER TABLE content_item DROP CONSTRAINT IF EXISTS"
        " ck_content_item_format")
    await conn.execute(
        "ALTER TABLE content_item ADD CONSTRAINT ck_content_item_format"
        f" CHECK (format IN {schema.E.sql_in(schema.E.CONTENT_FORMATS)})")


async def pg_migrate_17_to_18(conn: "psycopg.AsyncConnection") -> None:
    """v18 — the `content_render` job kind (re-render an edited reel). Widen the
    job-kind CHECK to the current JOB_KINDS. Constraint-only, no data migration."""
    await conn.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")


# Registry: version N -> async function taking N's schema to N+1's.
# Forward-only.
PG_MIGRATIONS: dict[
    int, Callable[["psycopg.AsyncConnection"], Awaitable[None]]] = {
    1: pg_migrate_1_to_2,
    2: pg_migrate_2_to_3,
    3: pg_migrate_3_to_4,
    4: pg_migrate_4_to_5,
    5: pg_migrate_5_to_6,
    6: pg_migrate_6_to_7,
    7: pg_migrate_7_to_8,
    8: pg_migrate_8_to_9,
    9: pg_migrate_9_to_10,
    10: pg_migrate_10_to_11,
    11: pg_migrate_11_to_12,
    12: pg_migrate_12_to_13,
    13: pg_migrate_13_to_14,
    14: pg_migrate_14_to_15,
    15: pg_migrate_15_to_16,
    16: pg_migrate_16_to_17,
    17: pg_migrate_17_to_18,
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
