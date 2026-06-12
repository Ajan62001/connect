"""PG storage core (v0.2 port P1): schema creation, advisory-lock migration
discipline, and the type-adaptation contracts every DAO will rely on in P2
(ISO-string timestamps, jsonb round-trips, booleans, generated tsvectors,
pgvector, named CHECKs, deferrable FKs, the active-edge partial unique).

Requires the db-test service (see tests/conftest.py) — skips loudly
otherwise. The SQLite suite is untouched by everything here.
"""

from __future__ import annotations

import asyncio
import re

import pytest

psycopg = pytest.importorskip("psycopg")

from pgvector import Vector  # noqa: E402

from connect.storage import migrations, pg  # noqa: E402
from connect.storage.schema import (  # noqa: E402
    PG_SCHEMA_VERSION,
    StorageError,
    StorageVersionError,
)

# Every table the baseline must create: the full v9/v10 set translated 1:1
# plus the tenancy tables (v02-tenancy-auth.md §2).
EXPECTED_TABLES = {
    "meta", "app_user", "user_session", "invite", "app_setting",
    "source", "document", "document_link", "document_topic",
    "document_enrichment", "claim", "evidence",
    "entity", "entity_mention", "event", "event_type", "event_assignment",
    "edge", "story", "claim_sighting", "verdict_history", "contradiction",
    "dossier", "dossier_section", "question", "finding", "finding_evidence",
    "statement", "position_shift", "view_summary",
    "job", "job_event", "watch", "watch_hit", "post", "workspace", "workspace_chat", "social_draft",
    "brief", "brief_item", "view_cursor", "calendar_event", "llm_call",
    "source_stats", "document_embedding", "event_embedding",
    "claim_embedding",
    "fetch_domain", "robots_cache", "beat_run",  # v2: runtime shared state
}


async def _insert_document(conn, content_hash: str, *, title=None,
                           content_text="body", **cols) -> int:
    names = ["fetched_at", "content_text", "content_hash", "title"]
    values = [pg.utc_now(), content_text, content_hash, title]
    for k, v in cols.items():
        names.append(k)
        values.append(v)
    cur = await conn.execute(
        f"INSERT INTO document ({', '.join(names)})"
        f" VALUES ({', '.join(['%s'] * len(names))}) RETURNING id",
        values)
    return (await cur.fetchone())["id"]


# --- bootstrap: create / migrate / race --------------------------------------

async def test_fresh_init_creates_full_schema(pg_fresh_dsn):
    version = await pg.init_db(pg_fresh_dsn)
    assert version == PG_SCHEMA_VERSION == 11

    conn = await pg.connect(pg_fresh_dsn)
    try:
        cur = await conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = {r["tablename"] for r in await cur.fetchall()}
        missing = EXPECTED_TABLES - tables
        assert not missing, f"missing tables: {missing}"
        assert tables == EXPECTED_TABLES  # nothing unexpected either

        cur = await conn.execute("SELECT extname FROM pg_extension")
        exts = {r["extname"] for r in await cur.fetchall()}
        assert {"vector", "pg_trgm"} <= exts

        # FTS5 + triggers are replaced by GENERATED tsvector columns
        for table in ("document", "claim", "entity"):
            cur = await conn.execute(
                "SELECT is_generated FROM information_schema.columns"
                " WHERE table_name = %s AND column_name = 'search_tsv'",
                (table,))
            row = await cur.fetchone()
            assert row is not None and row["is_generated"] == "ALWAYS", table

        cur = await conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        indexes = {r["indexname"] for r in await cur.fetchall()}
        for required in (
                "idx_edge_active_unique",
                "idx_document_tsv", "idx_claim_tsv", "idx_entity_tsv",
                "idx_entity_name_trgm", "idx_entity_aliases_trgm",
                "idx_document_embedding_hnsw", "idx_event_embedding_hnsw",
                "idx_claim_embedding_hnsw",
                "idx_document_owner", "idx_document_visibility_fetched",
                "idx_dossier_owner", "idx_dossier_visible",
                "idx_watch_user", "idx_llm_call_user_day",
                "idx_session_expires"):
            assert required in indexes, required

        # named constraints exist, incl. the circular-pair closer
        cur = await conn.execute(
            "SELECT conname FROM pg_constraint WHERE conname IN"
            " ('ck_document_media_type', 'ck_app_user_role',"
            "  'ck_job_kind', 'fk_dossier_parent_question')")
        assert len(await cur.fetchall()) == 4
    finally:
        await conn.close()


async def test_init_db_idempotent_and_version_round_trip(pg_fresh_dsn):
    assert await pg.init_db(pg_fresh_dsn) == PG_SCHEMA_VERSION
    # re-running against an existing DB is a no-op at the same version
    assert await pg.init_db(pg_fresh_dsn) == PG_SCHEMA_VERSION
    async with await psycopg.AsyncConnection.connect(pg_fresh_dsn) as conn:
        assert await migrations.pg_read_version(conn) == PG_SCHEMA_VERSION


async def test_migrate_1_to_2_walks_a_v1_database_forward(pg_fresh_dsn):
    """Forward-only migration coverage: reconstruct a v1 job table (drop
    the v2 queue columns/state tables), stamp version 1, and let init_db
    walk it forward (1 -> 2 -> 3) — columns appear, run_at backfills from
    created_at, the runtime tables exist."""
    await pg.init_db(pg_fresh_dsn)
    conn = await pg.connect(pg_fresh_dsn)
    try:
        # downgrade to the v1 shape
        await conn.execute("DROP TABLE fetch_domain, robots_cache, beat_run")
        await conn.execute(
            "ALTER TABLE job DROP COLUMN priority, DROP COLUMN max_attempts,"
            " DROP COLUMN run_at, DROP COLUMN claimed_by,"
            " DROP COLUMN heartbeat_at")
        await conn.execute(
            "UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        # a v1-era row whose run_at must backfill from created_at
        await conn.execute(
            "INSERT INTO job (kind, created_at) VALUES ('analysis', %s)",
            ("2026-01-02T03:04:05.000Z",))
        assert await pg.init_db(pg_fresh_dsn) == PG_SCHEMA_VERSION
        cur = await conn.execute(
            "SELECT priority, max_attempts, run_at, created_at,"
            " claimed_by, heartbeat_at FROM job")
        row = await cur.fetchone()
        assert (row["priority"], row["max_attempts"]) == (50, 1)
        assert row["run_at"] == row["created_at"]
        assert row["claimed_by"] is None and row["heartbeat_at"] is None
        cur = await conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'job'")
        indexes = {r["indexname"] for r in await cur.fetchall()}
        assert {"idx_job_claim", "idx_job_heartbeat",
                "uq_job_poll_source"} <= indexes
        for table in ("fetch_domain", "robots_cache", "beat_run"):
            cur = await conn.execute(
                "SELECT to_regclass(%s) AS t", (f"public.{table}",))
            assert (await cur.fetchone())["t"] == table
    finally:
        await conn.close()


async def test_refuses_newer_db(pg_fresh_dsn):
    await pg.init_db(pg_fresh_dsn)
    async with await psycopg.AsyncConnection.connect(pg_fresh_dsn) as conn:
        await conn.execute(
            "UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        await conn.commit()
    with pytest.raises(StorageVersionError):
        await pg.init_db(pg_fresh_dsn)


async def test_migration_race_two_concurrent_appliers(pg_fresh_dsn):
    """Two replicas race init_db on a fresh database: the advisory xact lock
    serializes them, the loser re-reads the version after acquiring it and
    no-ops — both succeed, one schema."""
    results = await asyncio.gather(
        pg.init_db(pg_fresh_dsn), pg.init_db(pg_fresh_dsn))
    assert results == [PG_SCHEMA_VERSION, PG_SCHEMA_VERSION]
    conn = await pg.connect(pg_fresh_dsn)
    try:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM meta WHERE key = 'schema_version'")
        assert (await cur.fetchone())["n"] == 1
    finally:
        await conn.close()


async def test_pg_apply_noop_and_unregistered_gap(pg_conn):
    assert await migrations.pg_apply(pg_conn, PG_SCHEMA_VERSION) \
        == PG_SCHEMA_VERSION
    with pytest.raises(StorageError):
        await migrations.pg_apply(pg_conn, 0)  # nothing registered for 0->1
    await pg_conn.rollback()


# --- type adaptation: the contracts the DAO port builds on -------------------

def test_utc_now_parity():
    """pg.utc_now keeps v0.1 db.utc_now's exact string shape."""
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", pg.utc_now())


async def test_timestamptz_round_trips_as_iso_string(pg_conn):
    now = pg.utc_now()
    await pg_conn.execute(
        "INSERT INTO source (name, type, created_at) VALUES (%s, 'rss', %s)",
        ("s1", now))
    cur = await pg_conn.execute(
        "SELECT created_at, last_polled_at FROM source WHERE name = 's1'")
    row = await cur.fetchone()
    assert row["created_at"] == now          # exact string round-trip
    assert row["last_polled_at"] is None
    # the column is a REAL timestamptz: date arithmetic works server-side
    cur = await pg_conn.execute(
        "SELECT count(*) AS n FROM source"
        " WHERE created_at >= now() - interval '1 day'")
    assert (await cur.fetchone())["n"] == 1


async def test_date_round_trips_as_iso_date_string(pg_conn):
    await pg_conn.execute(
        "INSERT INTO calendar_event (kind, occurs_on, label)"
        " VALUES ('budget', %s, 'Union Budget')", ("2026-02-01",))
    cur = await pg_conn.execute(
        "SELECT occurs_on, ends_on FROM calendar_event")
    row = await cur.fetchone()
    assert row["occurs_on"] == "2026-02-01"  # 10-char v0.1 shape, not a date
    assert row["ends_on"] is None


async def test_jsonb_round_trip(pg_conn):
    config = {"url": "https://example.org/feed", "max_items": 3}
    await pg_conn.execute(
        "INSERT INTO source (name, type, config, created_at)"
        " VALUES ('s2', 'rss', %s, %s)", (pg.Jsonb(config), pg.utc_now()))
    cur = await pg_conn.execute(
        "SELECT config FROM source WHERE name = 's2'")
    assert (await cur.fetchone())["config"] == config  # parsed dict, no loads
    # jsonb defaults arrive as parsed objects too
    await pg_conn.execute(
        "INSERT INTO entity (name, created_at) VALUES ('SEBI', %s)",
        (pg.utc_now(),))
    cur = await pg_conn.execute(
        "SELECT aliases, attrs FROM entity WHERE name = 'SEBI'")
    row = await cur.fetchone()
    assert row["aliases"] == [] and row["attrs"] == {}


async def test_boolean_and_single_user_defaults(pg_conn):
    """A bare v0.1-shaped insert works: tenancy columns take single-user-safe
    defaults; INTEGER-boolean columns are real bools now."""
    doc_id = await _insert_document(pg_conn, "h-defaults")
    cur = await pg_conn.execute(
        "SELECT media_type, enrichment_status, watch_hit, owner_id,"
        " visibility, origin FROM document WHERE id = %s", (doc_id,))
    row = await cur.fetchone()
    assert row["media_type"] == "html"
    assert row["enrichment_status"] == "pending"
    assert row["watch_hit"] is False
    assert row["owner_id"] is None           # NULL = system / single-user
    assert row["visibility"] == "shared"     # locked decision
    assert row["origin"] == "polled"

    await pg_conn.execute(
        "INSERT INTO job (kind, created_at) VALUES ('analysis', %s)",
        (pg.utc_now(),))
    cur = await pg_conn.execute(
        "SELECT status, attempts, cancel_requested, payload, owner_id"
        " FROM job")
    row = await cur.fetchone()
    assert row["status"] == "queued" and row["attempts"] == 0
    assert row["cancel_requested"] is False
    assert row["payload"] == {} and row["owner_id"] is None


async def test_named_check_constraint_violation(pg_conn):
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        await _insert_document(pg_conn, "h-bad", media_type="carrier_pigeon")
    assert exc_info.value.diag.constraint_name == "ck_document_media_type"
    await pg_conn.rollback()


# --- generated tsvector search (FTS5 replacement) -----------------------------

async def test_document_tsv_generated_and_searchable(pg_conn):
    doc_id = await _insert_document(
        pg_conn, "h-fts", title="RBI update",
        content_text="the digital rupee pilot expands")
    cur = await pg_conn.execute(
        "SELECT d.id, ts_rank_cd(d.search_tsv, q) AS rank"
        " FROM document d, websearch_to_tsquery('english', %s) q"
        " WHERE d.search_tsv @@ q", ("rupee pilot",))
    rows = await cur.fetchall()
    assert [r["id"] for r in rows] == [doc_id]
    assert rows[0]["rank"] > 0
    # websearch_to_tsquery never raises on hostile input — safe_match dies
    cur = await pg_conn.execute(
        "SELECT count(*) AS n FROM document d,"
        " websearch_to_tsquery('english', %s) q WHERE d.search_tsv @@ q",
        ('"unbalanced AND ((( quotes', ))
    assert (await cur.fetchone())["n"] == 0


async def test_claim_and_entity_tsv(pg_conn):
    cur = await pg_conn.execute(
        "INSERT INTO claim (text, created_at) VALUES (%s, %s) RETURNING id",
        ("The digital rupee pilot covers 13 banks", pg.utc_now()))
    claim_id = (await cur.fetchone())["id"]
    cur = await pg_conn.execute(
        "SELECT id FROM claim WHERE search_tsv @@"
        " websearch_to_tsquery('english', 'banks')")
    assert (await cur.fetchone())["id"] == claim_id

    # entity tsv covers name AND the aliases jsonb (cast to text in the
    # generated column; 'simple' config — proper names are not stemmed)
    await pg_conn.execute(
        "INSERT INTO entity (name, entity_type, aliases, created_at)"
        " VALUES (%s, 'organization', %s, %s)",
        ("Reserve Bank of India", pg.Jsonb(["RBI", "central bank"]),
         pg.utc_now()))
    for query in ("RBI", "Reserve"):
        cur = await pg_conn.execute(
            "SELECT name FROM entity WHERE search_tsv @@"
            " websearch_to_tsquery('simple', %s)", (query,))
        row = await cur.fetchone()
        assert row is not None and row["name"] == "Reserve Bank of India", query
    # ILIKE substring parity path (served by the trigram indexes at scale)
    cur = await pg_conn.execute(
        r"SELECT count(*) AS n FROM entity WHERE name ILIKE %s ESCAPE '\'",
        ("%reserve%",))
    assert (await cur.fetchone())["n"] == 1


# --- pgvector -----------------------------------------------------------------

async def test_pgvector_round_trip_and_nearest(pg_conn):
    d1 = await _insert_document(pg_conn, "h-v1")
    d2 = await _insert_document(pg_conn, "h-v2")
    e1 = [1.0] + [0.0] * 383
    e2 = [0.0, 1.0] + [0.0] * 382
    for doc_id, vec in ((d1, e1), (d2, e2)):
        await pg_conn.execute(
            "INSERT INTO document_embedding (document_id, model, embedding)"
            " VALUES (%s, %s, %s)", (doc_id, "bge-small", Vector(vec)))
    probe = Vector([0.9, 0.1] + [0.0] * 382)
    cur = await pg_conn.execute(
        "SELECT document_id, 1 - (embedding <=> %s) AS sim"
        " FROM document_embedding ORDER BY embedding <=> %s LIMIT 2",
        (probe, probe))
    rows = await cur.fetchall()
    assert [r["document_id"] for r in rows] == [d1, d2]
    assert rows[0]["sim"] > rows[1]["sim"] > 0
    cur = await pg_conn.execute(
        "SELECT embedding FROM document_embedding WHERE document_id = %s",
        (d1,))
    stored = (await cur.fetchone())["embedding"]
    assert len(stored) == 384 and float(stored[0]) == 1.0

    # vector(384) enforces the dimension — the v0.1 dim column is dead
    with pytest.raises(psycopg.errors.DataException):
        await pg_conn.execute(
            "INSERT INTO event_embedding (event_id, model, embedding)"
            " VALUES (1, 'bge-small', %s)", (Vector([1.0, 2.0]),))
    await pg_conn.rollback()


# --- graph constraints ----------------------------------------------------------

async def test_edge_active_partial_unique_and_scd2(pg_conn):
    insert = (
        "INSERT INTO edge (src_type, src_id, dst_type, dst_id, relation,"
        " created_at) VALUES ('event', 1, 'event', 2, 'follows', %s)"
        " ON CONFLICT (src_type, src_id, dst_type, dst_id, relation)"
        " WHERE status = 'active' DO NOTHING RETURNING id")
    cur = await pg_conn.execute(insert, (pg.utc_now(),))
    first = await cur.fetchone()
    assert first is not None
    # duplicate ACTIVE edge: idempotent no-op (edges.insert returns None)
    cur = await pg_conn.execute(insert, (pg.utc_now(),))
    assert await cur.fetchone() is None
    # SCD2: close the old edge, the same relation may go active again
    await pg_conn.execute(
        "UPDATE edge SET status = 'superseded' WHERE id = %s",
        (first["id"],))
    cur = await pg_conn.execute(insert, (pg.utc_now(),))
    again = await cur.fetchone()
    assert again is not None and again["id"] != first["id"]


async def test_deferrable_self_fk_for_etl(pg_conn):
    """document.canonical_document_id is DEFERRABLE INITIALLY IMMEDIATE:
    immediate by default, deferrable inside the ETL's one big transaction."""
    # immediate mode: a dangling reference fails at statement time
    doc_a = await _insert_document(pg_conn, "h-fk-a")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await pg_conn.execute(
            "UPDATE document SET canonical_document_id = %s WHERE id = %s",
            (doc_a + 999, doc_a))
    await pg_conn.rollback()

    # deferred mode (SET CONSTRAINTS ALL DEFERRED): dangling mid-transaction
    # is fine as long as commit-time state is consistent
    doc_a = await _insert_document(pg_conn, "h-fk-a2")
    async with pg_conn.transaction():
        await pg_conn.execute("SET CONSTRAINTS ALL DEFERRED")
        await pg_conn.execute(
            "UPDATE document SET canonical_document_id = %s WHERE id = %s",
            (doc_a + 999, doc_a))                      # dangling, tolerated
        doc_b = await _insert_document(pg_conn, "h-fk-b")
        await pg_conn.execute(
            "UPDATE document SET canonical_document_id = %s WHERE id = %s",
            (doc_b, doc_a))                            # consistent at commit
    cur = await pg_conn.execute(
        "SELECT canonical_document_id FROM document WHERE id = %s", (doc_a,))
    assert (await cur.fetchone())["canonical_document_id"] == doc_b


# --- tenancy baseline ------------------------------------------------------------

async def test_tenancy_tables_and_session_cascade(pg_conn):
    cur = await pg_conn.execute(
        "INSERT INTO app_user (email, created_at) VALUES (%s, %s)"
        " RETURNING id, role, disabled",
        ("admin@example.org", pg.utc_now()))
    user = await cur.fetchone()
    assert user["role"] == "member" and user["disabled"] is False

    await pg_conn.execute(
        "INSERT INTO user_session (id, user_id, created_at, expires_at)"
        " VALUES ('tok', %s, %s, %s)",
        (user["id"], pg.utc_now(), pg.utc_now()))
    await pg_conn.execute(
        "INSERT INTO invite (email, invited_by, created_at)"
        " VALUES ('m@example.org', %s, %s)", (user["id"], pg.utc_now()))
    await pg_conn.execute(
        "INSERT INTO app_setting (key, value) VALUES ('daily_budget', '10')"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    await pg_conn.commit()  # keep rows past the failed-delete rollback below

    # invite.invited_by is a plain FK (no cascade): deleting the inviter is
    # blocked while the invite row exists
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await pg_conn.execute(
            "DELETE FROM app_user WHERE id = %s", (user["id"],))
    await pg_conn.rollback()
    await pg_conn.execute("DELETE FROM invite WHERE email = 'm@example.org'")
    await pg_conn.execute("DELETE FROM app_user WHERE id = %s", (user["id"],))
    cur = await pg_conn.execute("SELECT count(*) AS n FROM user_session")
    assert (await cur.fetchone())["n"] == 0  # ON DELETE CASCADE


async def _make_user(pg_conn, email: str, role: str = "member") -> int:
    cur = await pg_conn.execute(
        "INSERT INTO app_user (email, role, created_at)"
        " VALUES (%s, %s, %s) RETURNING id", (email, role, pg.utc_now()))
    return int((await cur.fetchone())["id"])


async def test_brief_unique_per_user_day(pg_conn):
    """v3: one brief per (user, day) — the conflict target guarding
    concurrent first-GET generation; different users coexist on a day."""
    alice = await _make_user(pg_conn, "alice@example.org")
    bob = await _make_user(pg_conn, "bob@example.org")
    insert = ("INSERT INTO brief (user_id, brief_date, generated_at)"
              " VALUES (%s, %s, %s)"
              " ON CONFLICT (user_id, brief_date) DO NOTHING")
    cur = await pg_conn.execute(insert, (alice, "2026-06-11", pg.utc_now()))
    assert cur.rowcount == 1
    cur = await pg_conn.execute(insert, (alice, "2026-06-11", pg.utc_now()))
    assert cur.rowcount == 0                 # loser of the race no-ops
    cur = await pg_conn.execute(insert, (bob, "2026-06-11", pg.utc_now()))
    assert cur.rowcount == 1                 # per-user, not global
    cur = await pg_conn.execute("SELECT count(*) AS n FROM brief")
    assert (await cur.fetchone())["n"] == 2


async def test_view_cursor_upsert_shape(pg_conn):
    """v3: the PK is (user_id, surface, ref_id) — per-user cursors that
    never collide across users."""
    alice = await _make_user(pg_conn, "alice@example.org")
    bob = await _make_user(pg_conn, "bob@example.org")
    upsert = ("INSERT INTO view_cursor (user_id, surface, ref_id,"
              " last_seen_at) VALUES (%s, 'entity', 7, %s)"
              " ON CONFLICT (user_id, surface, ref_id)"
              " DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at")
    t1, t2 = pg.utc_now(), pg.utc_now()
    await pg_conn.execute(upsert, (alice, t1))
    await pg_conn.execute(upsert, (alice, t2))
    await pg_conn.execute(upsert, (bob, t1))
    cur = await pg_conn.execute(
        "SELECT user_id, last_seen_at FROM view_cursor ORDER BY user_id")
    rows = await cur.fetchall()
    assert len(rows) == 2                    # alice upserted, bob separate
    assert rows[0]["user_id"] == alice and rows[0]["last_seen_at"] == t2
    assert rows[1]["user_id"] == bob


async def test_migrate_2_to_3_backfills_to_first_admin(pg_fresh_dsn):
    """The v2 -> v3 tenancy tightening: pre-tenancy NULL owner/user rows
    backfill to the FIRST ADMIN, the columns go NOT NULL, view_cursor's PK
    becomes (user_id, surface, ref_id)."""
    await pg.init_db(pg_fresh_dsn)
    conn = await pg.connect(pg_fresh_dsn)
    try:
        # downgrade to the v2 shape: old PK first (user_id must leave the
        # PK before it can go nullable), then nullable columns
        await conn.execute(
            "ALTER TABLE view_cursor DROP CONSTRAINT view_cursor_pkey")
        await conn.execute(
            "ALTER TABLE view_cursor ADD PRIMARY KEY (surface, ref_id)")
        await conn.execute(
            "ALTER TABLE dossier ALTER COLUMN owner_id DROP NOT NULL")
        for table in ("watch", "brief", "view_cursor"):
            await conn.execute(
                f"ALTER TABLE {table} ALTER COLUMN user_id DROP NOT NULL")
        await conn.execute(
            "UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        # single-user-era rows with NULL ownership + the admin to inherit
        admin = await _make_user(conn, "admin@example.org", role="admin")
        await conn.execute(
            "INSERT INTO dossier (input_text, status, created_at)"
            " VALUES ('orphan', 'completed', %s)", (pg.utc_now(),))
        await conn.execute(
            "INSERT INTO watch (kind, label, query_fts, created_at)"
            " VALUES ('topic', 'w', 'q', %s)", (pg.utc_now(),))
        await conn.execute(
            "INSERT INTO brief (brief_date, generated_at) VALUES (%s, %s)",
            ("2026-06-10", pg.utc_now()))
        await conn.execute(
            "INSERT INTO view_cursor (surface, ref_id, last_seen_at)"
            " VALUES ('entity', 1, %s)", (pg.utc_now(),))

        assert await pg.init_db(pg_fresh_dsn) == PG_SCHEMA_VERSION == 11

        for table, col in (("dossier", "owner_id"), ("watch", "user_id"),
                           ("brief", "user_id"),
                           ("view_cursor", "user_id")):
            cur = await conn.execute(f"SELECT {col} AS v FROM {table}")
            assert (await cur.fetchone())["v"] == admin, table
            cur = await conn.execute(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = %s AND column_name = %s",
                (table, col))
            assert (await cur.fetchone())["is_nullable"] == "NO", table
        # the PK swap landed: same user+surface+ref upserts, users coexist
        cur = await conn.execute(
            "SELECT a.attname FROM pg_index i"
            " JOIN pg_attribute a ON a.attrelid = i.indrelid"
            "   AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = 'view_cursor'::regclass AND i.indisprimary")
        pk_cols = {r["attname"] for r in await cur.fetchall()}
        assert pk_cols == {"user_id", "surface", "ref_id"}
    finally:
        await conn.close()
