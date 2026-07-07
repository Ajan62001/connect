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


async def pg_migrate_18_to_19(conn: "psycopg.AsyncConnection") -> None:
    """v19 — S1, editorial-integrity suite: the `content_item_source` provenance
    link table tying each content_item back to the source documents it was
    grounded in (verbatim quote + char offsets + credibility-tier snapshot).
    Fresh DDL, then a ONE-TIME idempotent backfill of link rows from the
    existing JSONB `content_item.sources` so the reverse index (S3/S4/S6) is not
    blind to pre-existing items. The credibility_tier is resolved from each
    cited document's registered source at backfill time."""
    for ddl in (schema._PG_DDL_CONTENT_ITEM_SOURCE,
                *schema._PG_DDL_CONTENT_ITEM_SOURCE_INDEXES):
        await conn.execute(ddl)
    await conn.execute(
        """
        INSERT INTO content_item_source
            (content_item_id, ref, document_id, finding_id, source_name,
             title, url, quote, quote_start, quote_end, occurred_on,
             credibility_tier, created_at)
        SELECT ci.id,
               COALESCE(s->>'ref', ''),
               NULLIF(s->>'document_id', '')::bigint,
               NULLIF(s->>'finding_id', '')::bigint,
               s->>'source_name', s->>'title', s->>'url', s->>'quote',
               NULLIF(s->>'quote_start', '')::int,
               NULLIF(s->>'quote_end', '')::int,
               s->>'occurred_on',
               (SELECT src.credibility_tier FROM source src
                  JOIN document d ON d.source_id = src.id
                 WHERE d.id = NULLIF(s->>'document_id', '')::bigint),
               COALESCE(ci.created_at, now())
          FROM content_item ci
          CROSS JOIN LATERAL jsonb_array_elements(ci.sources) AS s
         WHERE jsonb_typeof(ci.sources) = 'array'
           AND NOT EXISTS (SELECT 1 FROM content_item_source cis
                            WHERE cis.content_item_id = ci.id)
        """)


async def pg_migrate_19_to_20(conn: "psycopg.AsyncConnection") -> None:
    """v20 — S2, publish-time editorial verification gate. New vocabulary
    ('verifying'/'flagged' content_item statuses, the 'content_verify' job
    kind), the `gate` jsonb report column on content_item, and provenance
    (`sources`/`grounding`) on social_draft so the (formerly unverified) social
    path can be hard-delegated through the same closed-menu gate. Constraint
    widening + additive columns; no data migration. A partial unique index
    deduplicates in-flight content_verify jobs per item."""
    await conn.execute(
        "ALTER TABLE content_item DROP CONSTRAINT IF EXISTS"
        " ck_content_item_status")
    await conn.execute(
        "ALTER TABLE content_item ADD CONSTRAINT ck_content_item_status"
        f" CHECK (status IN {schema.E.sql_in(schema.E.CONTENT_ITEM_STATUSES)})")
    await conn.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")
    await conn.execute(
        "ALTER TABLE content_item ADD COLUMN IF NOT EXISTS gate"
        " jsonb NOT NULL DEFAULT '{}'::jsonb")
    await conn.execute(
        "ALTER TABLE social_draft ADD COLUMN IF NOT EXISTS sources"
        " jsonb NOT NULL DEFAULT '[]'::jsonb")
    await conn.execute(
        "ALTER TABLE social_draft ADD COLUMN IF NOT EXISTS grounding"
        " jsonb NOT NULL DEFAULT '{}'::jsonb")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_content_verify"
        " ON job ((payload->>'item_id'))"
        " WHERE kind = 'content_verify' AND status = 'queued'")


async def pg_migrate_20_to_21(conn: "psycopg.AsyncConnection") -> None:
    """v21 — S3, corrections / retractions / verdict propagation. The
    `correction` + `content_item_correction` tables, the recheck cursor columns
    on `source`, the 'retracted'/'corrected' content_item statuses, and the
    'content_correction'/'source_recheck' job kinds. Constraint widening +
    additive tables/columns; no data migration. A partial unique index keeps at
    most one content_correction sweep job in flight."""
    for ddl in (schema._PG_DDL_CORRECTION, *schema._PG_DDL_CORRECTION_INDEXES,
                schema._PG_DDL_CONTENT_ITEM_CORRECTION,
                *schema._PG_DDL_CONTENT_ITEM_CORRECTION_INDEXES):
        await conn.execute(ddl)
    await conn.execute(
        "ALTER TABLE source ADD COLUMN IF NOT EXISTS last_rechecked_at"
        " timestamptz")
    await conn.execute(
        "ALTER TABLE source ADD COLUMN IF NOT EXISTS canonical_hash text")
    await conn.execute(
        "ALTER TABLE content_item DROP CONSTRAINT IF EXISTS"
        " ck_content_item_status")
    await conn.execute(
        "ALTER TABLE content_item ADD CONSTRAINT ck_content_item_status"
        f" CHECK (status IN {schema.E.sql_in(schema.E.CONTENT_ITEM_STATUSES)})")
    await conn.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_content_correction"
        " ON job ((kind)) WHERE kind = 'content_correction'"
        " AND status IN ('queued','running')")


async def pg_migrate_21_to_22(conn: "psycopg.AsyncConnection") -> None:
    """v22 — S4, dynamic source-credibility scoring. Adds the reliability_score
    columns to `source`, the `source_credibility_history` audit table, and the
    'credibility_recompute' job kind. Additive columns/table + constraint
    widening; no data migration (NULL reliability falls back to the tier prior,
    so existing verdicts are unchanged until the first recompute runs)."""
    await conn.execute(
        "ALTER TABLE source ADD COLUMN IF NOT EXISTS reliability_score"
        " double precision")
    await conn.execute(
        "ALTER TABLE source ADD COLUMN IF NOT EXISTS reliability_updated_at"
        " timestamptz")
    for ddl in (schema._PG_DDL_SOURCE_CREDIBILITY_HISTORY,
                *schema._PG_DDL_SOURCE_CREDIBILITY_HISTORY_INDEXES):
        await conn.execute(ddl)
    await conn.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")


async def pg_migrate_22_to_23(conn: "psycopg.AsyncConnection") -> None:
    """v23 — S5, production integrity observability + live eval gate. The
    `integrity_event` (append-only signal stream) and `integrity_eval_run`
    (scheduled live-eval ledger) tables, and the 'integrity_eval' job kind.
    Additive tables + constraint widening; no data migration."""
    for ddl in (schema._PG_DDL_INTEGRITY_EVENT,
                *schema._PG_DDL_INTEGRITY_EVENT_INDEXES,
                schema._PG_DDL_INTEGRITY_EVAL_RUN,
                *schema._PG_DDL_INTEGRITY_EVAL_RUN_INDEXES):
        await conn.execute(ddl)
    await conn.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")


async def pg_migrate_23_to_24(conn: "psycopg.AsyncConnection") -> None:
    """v24 — the 'meme' content format (stock photo + top/bottom caption,
    publishes to Instagram like ig_card), plus a NARROWER content_verify dedup
    window: the unique index covers only QUEUED jobs, so an enqueue for an
    edit/approve made while a verify is RUNNING survives it (the running job
    snapshotted its item row at claim time and must not swallow it).
    Constraint widening + index re-create; no data migration."""
    await conn.execute(
        "ALTER TABLE content_item DROP CONSTRAINT IF EXISTS"
        " ck_content_item_format")
    await conn.execute(
        "ALTER TABLE content_item ADD CONSTRAINT ck_content_item_format"
        f" CHECK (format IN {schema.E.sql_in(schema.E.CONTENT_FORMATS)})")
    await conn.execute("DROP INDEX IF EXISTS uq_job_content_verify")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_content_verify"
        " ON job ((payload->>'item_id'))"
        " WHERE kind = 'content_verify' AND status = 'queued'")


async def pg_migrate_24_to_25(conn: "psycopg.AsyncConnection") -> None:
    """v25 — the editorial planner ('auto' campaigns). ``campaign.plan``
    stores the EditorialPlan the orchestrator produced when the request left
    format choice to the system (formats = []): significance score, format
    picks and per-format media treatment. NULL for user-picked campaigns.
    Additive column; no data migration."""
    await conn.execute(
        "ALTER TABLE campaign ADD COLUMN IF NOT EXISTS plan jsonb")


async def pg_migrate_25_to_26(conn: "psycopg.AsyncConnection") -> None:
    """v26 — the reel factory: the 'reel_factory' job kind (an autonomous
    production run that scouts hot subjects from the feed and commissions one
    reel-led campaign per pick) plus its singleton dedup index (at most one
    live run). Constraint widening + one index; no data migration."""
    await conn.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS ck_job_kind")
    await conn.execute(
        "ALTER TABLE job ADD CONSTRAINT ck_job_kind"
        f" CHECK (kind IN {schema.E.sql_in(schema.E.JOB_KINDS)})")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_reel_factory"
        " ON job ((kind)) WHERE kind = 'reel_factory'"
        " AND status IN ('queued','running')")


async def pg_migrate_26_to_27(conn: "psycopg.AsyncConnection") -> None:
    """v27 — workspaces become channel consoles: a shared ``character`` roster
    and ``script_preset`` table, a 1:1 ``workspace_channel`` binding (publishing
    account + default voice/character/script-type), and a ``campaign.workspace_id``
    column so a campaign's channel-of-record is orthogonal to its subject seed.
    All additive; backfills workspace_id from the seed JSONB where the referenced
    workspace still exists."""
    await conn.execute(schema._PG_DDL_CHARACTER)
    for ddl in schema._PG_DDL_CHARACTER_INDEXES:
        await conn.execute(ddl)
    await conn.execute(schema._PG_DDL_SCRIPT_PRESET)
    for ddl in schema._PG_DDL_SCRIPT_PRESET_INDEXES:
        await conn.execute(ddl)
    await conn.execute(schema._PG_DDL_WORKSPACE_CHANNEL)
    await conn.execute(
        "ALTER TABLE campaign ADD COLUMN IF NOT EXISTS workspace_id bigint"
        " REFERENCES workspace(id) ON DELETE SET NULL")
    # backfill from the seed, guarded against deleted workspaces (the FK would
    # otherwise reject) — campaigns of dropped workspaces just stay NULL
    await conn.execute(
        "UPDATE campaign SET workspace_id = (seed->>'workspace_id')::bigint"
        " WHERE workspace_id IS NULL AND seed->>'workspace_id' ~ '^[0-9]+$'"
        " AND EXISTS (SELECT 1 FROM workspace w"
        "             WHERE w.id = (seed->>'workspace_id')::bigint)")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_workspace"
        " ON campaign(workspace_id, created_at DESC)"
        " WHERE workspace_id IS NOT NULL")


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
    18: pg_migrate_18_to_19,
    19: pg_migrate_19_to_20,
    20: pg_migrate_20_to_21,
    21: pg_migrate_21_to_22,
    22: pg_migrate_22_to_23,
    23: pg_migrate_23_to_24,
    24: pg_migrate_24_to_25,
    25: pg_migrate_25_to_26,
    26: pg_migrate_26_to_27,
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
