"""Forward migrations: v1 -> v2 (document_link lands) and v2 -> v3 (the
source.type / document.media_type CHECK-vocabulary rebuild) — old data
intact, migrated DDL byte-identical to fresh-create — on synthetic old DBs
and (skip-if-missing) on a COPY of the real Phase 0 database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from connect.storage import db as db_mod
from connect.storage import migrations
from connect.storage import schema
from connect.storage.schema import SCHEMA_VERSION

LIVE_DB = Path(__file__).resolve().parents[1] / "data" / "connect.db"


def _make_v1_db(path) -> None:
    """Recreate a v1-versioned database: the current schema minus
    document_link (the v1 -> v2 delta; the v2 -> v3 CHECK rebuild then runs
    over it as a no-op copy)."""
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    with conn:
        conn.execute("DROP TABLE document_link")
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    # seed some v1-era data that must survive
    with conn:
        conn.execute(
            "INSERT INTO source (name, type, credibility_tier, created_at)"
            " VALUES ('RBI Notifications', 'rss', 1, '2026-06-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO document (source_id, url, title, fetched_at,"
            " media_type, content_text, content_hash)"
            " VALUES (1, 'https://rbi.org.in/x', 'Old circular',"
            " '2026-06-01T00:00:00Z', 'html', 'old body text', 'h-old')")
    conn.close()


# The v2 CHECK vocabularies, derived from the current DDL by removing the
# v3 + v5 additions — guarded so a future vocab change can't silently no-op.
_V2_SOURCE_DDL = schema.SOURCE_TABLE_DDL.replace(", 'twitter', 'telegram')", ")")
_V2_DOCUMENT_DDL = schema.DOCUMENT_TABLE_DDL.replace(
    ", 'tweet', 'telegram', 'xlsx', 'docx')", ")")
assert _V2_SOURCE_DDL != schema.SOURCE_TABLE_DDL
assert _V2_DOCUMENT_DDL != schema.DOCUMENT_TABLE_DDL


def _make_v2_db(path) -> None:
    """Recreate a v2 database: the current schema with the v2 CHECK vocab on
    source.type / document.media_type (the v2 -> v3 delta)."""
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    with conn:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE source")
        conn.execute(_V2_SOURCE_DDL)
        conn.execute("DROP TABLE document")  # drops its indexes + triggers
        conn.execute(_V2_DOCUMENT_DDL)
        for ddl in (*schema.DOCUMENT_INDEX_DDL,
                    *schema.DOCUMENT_FTS_TRIGGER_DDL):
            conn.execute(ddl)
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        # v2-era data that must survive (incl. a document_link child row)
        conn.execute(
            "INSERT INTO source (name, type, credibility_tier, created_at)"
            " VALUES ('RBI Notifications', 'rss', 1, '2026-06-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO document (source_id, url, title, fetched_at,"
            " media_type, content_text, content_hash)"
            " VALUES (1, 'https://rbi.org.in/x', 'Old circular',"
            " '2026-06-01T00:00:00Z', 'html', 'old body text', 'h-old')")
        conn.execute(
            "INSERT INTO document_link (document_id, url, created_at)"
            " VALUES (1, 'https://rbi.org.in/doc.pdf', '2026-06-01T00:00:00Z')")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()


def test_migrate_v1_to_v3(tmp_path):
    db_path = tmp_path / "v1.db"
    _make_v1_db(db_path)

    conn = db_mod.connect(db_path)
    assert migrations.read_version(conn) == 1
    version = db_mod.init_db(conn)
    assert version == SCHEMA_VERSION == 7
    assert migrations.read_version(conn) == 7

    # document_link exists, matching the fresh-create schema (same DDL)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "document_link" in tables
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND tbl_name='document_link'")}
    assert "idx_document_link_document" in indexes
    assert "idx_document_link_resolved" in indexes

    # old data intact
    row = conn.execute(
        "SELECT title, content_text FROM document WHERE id=1").fetchone()
    assert (row["title"], row["content_text"]) == ("Old circular",
                                                   "old body text")
    # FTS survived too
    hit = conn.execute("SELECT rowid FROM document_fts"
                       " WHERE document_fts MATCH 'circular'").fetchone()
    assert hit is not None

    # the new table is usable (status CHECK + unique constraint live)
    with conn:
        conn.execute(
            "INSERT INTO document_link (document_id, url, created_at)"
            " VALUES (1, 'https://rbi.org.in/doc.pdf', '2026-06-11T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO document_link (document_id, url) VALUES"
            " (1, 'https://rbi.org.in/doc.pdf')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO document_link (document_id, url, status) VALUES"
            " (1, 'https://other', 'bogus')")
    conn.close()


def test_migrate_v2_to_v3(tmp_path):
    db_path = tmp_path / "v2.db"
    _make_v2_db(db_path)

    conn = db_mod.connect(db_path)
    assert migrations.read_version(conn) == 2
    # the v2 vocab really was in force before migrating
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source (name, type, credibility_tier, created_at)"
            " VALUES ('x', 'twitter', 1, '2026-06-11T00:00:00Z')")
    conn.rollback()  # the failed INSERT left an implicit transaction open

    version = db_mod.init_db(conn)
    assert version == SCHEMA_VERSION == 7
    assert migrations.read_version(conn) == 7

    # old data intact across the rebuild (rowids preserved)
    row = conn.execute(
        "SELECT id, name, type FROM source WHERE id=1").fetchone()
    assert (row["name"], row["type"]) == ("RBI Notifications", "rss")
    row = conn.execute(
        "SELECT title, content_text, source_id FROM document WHERE id=1"
    ).fetchone()
    assert (row["title"], row["content_text"], row["source_id"]) == (
        "Old circular", "old body text", 1)
    row = conn.execute(
        "SELECT url FROM document_link WHERE document_id=1").fetchone()
    assert row["url"] == "https://rbi.org.in/doc.pdf"
    # FTS index survived the rebuild and the recreated triggers still fire
    assert conn.execute("SELECT rowid FROM document_fts"
                        " WHERE document_fts MATCH 'circular'").fetchone()
    # backup scratch tables are gone
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {t for t in tables if t.startswith("_mig_")}

    # the new vocabularies are live...
    with conn:
        conn.execute(
            "INSERT INTO source (name, type, config, credibility_tier,"
            " created_at) VALUES ('X gov', 'twitter', '{}', 1,"
            " '2026-06-11T00:00:00Z')")
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash, title) VALUES ('2026-06-11T00:00:00Z', 'tweet',"
            " 'a tweet about the digital rupee', 'h-tweet', 'tweet doc')")
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'telegram',"
            " 'a telegram post', 'h-tg')")
    # ...the recreated FTS triggers index new rows...
    assert conn.execute("SELECT rowid FROM document_fts"
                        " WHERE document_fts MATCH 'rupee'").fetchone()
    # ...and the CHECKs still reject garbage
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source (name, type, credibility_tier, created_at)"
            " VALUES ('bad', 'mastodon', 1, '2026-06-11T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'bogus', 'x', 'h-b')")
    conn.close()


def test_migration_matches_fresh_schema(tmp_path):
    """Migrated v1 and v2 DBs declare byte-identical tables to a fresh v3 DB
    (the rebuild executes the exact fresh-create DDL strings)."""
    migrated_v1 = tmp_path / "m1.db"
    _make_v1_db(migrated_v1)
    conn_m1 = db_mod.connect(migrated_v1)
    db_mod.init_db(conn_m1)

    migrated_v2 = tmp_path / "m2.db"
    _make_v2_db(migrated_v2)
    conn_m2 = db_mod.connect(migrated_v2)
    db_mod.init_db(conn_m2)

    conn_f = db_mod.connect(tmp_path / "f.db")
    db_mod.init_db(conn_f)

    def ddl(conn, name):
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()[0]

    for table in ("source", "document", "document_link"):
        assert ddl(conn_m1, table) == ddl(conn_f, table)
        assert ddl(conn_m2, table) == ddl(conn_f, table)
    conn_m1.close()
    conn_m2.close()
    conn_f.close()


@pytest.mark.skipif(not LIVE_DB.exists(), reason="no live Phase 0 DB here")
def test_migrate_copy_of_live_db(tmp_path):
    """Upgrade a COPY of the real corpus DB in place; never touches the
    original (read-only source + sqlite backup API, WAL-safe)."""
    copy_path = tmp_path / "live-copy.db"
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(str(copy_path))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    conn = db_mod.connect(copy_path)
    docs_before = conn.execute("SELECT COUNT(*) FROM document").fetchone()[0]
    sources_before = conn.execute("SELECT COUNT(*) FROM source").fetchone()[0]
    links_before = conn.execute(
        "SELECT COUNT(*) FROM document_link").fetchone()[0] \
        if conn.execute("SELECT name FROM sqlite_master WHERE"
                        " name='document_link'").fetchone() else None

    version = db_mod.init_db(conn)  # migrates if the copy is still v1/v2
    assert version == SCHEMA_VERSION == 7

    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] \
        == docs_before
    assert conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] \
        == sources_before
    if links_before is not None:
        assert conn.execute(
            "SELECT COUNT(*) FROM document_link").fetchone()[0] == links_before
    # the rebuilt tables accept the new vocab on the copy (v3 + v5 additions)
    with conn:
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'tweet',"
            " 'migration smoke tweet', 'h-migration-smoke')")
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'xlsx',"
            " 'migration smoke spreadsheet', 'h-migration-smoke-xlsx')")
    # FTS stayed consistent: the new rows are searchable via the recreated
    # triggers and old rows are still indexed
    assert conn.execute(
        "SELECT rowid FROM document_fts WHERE document_fts MATCH"
        " '\"migration smoke tweet\"'").fetchone()
    assert conn.execute(
        "SELECT rowid FROM document_fts WHERE document_fts MATCH"
        " '\"migration smoke spreadsheet\"'").fetchone()
    conn.close()


# --- v3 -> v4 (Phase 1: document_enrichment + job.kind vocab) -----------------

# The v3 job CHECK vocabulary, derived from the current DDL by removing the
# v4 addition — guarded so a future vocab change can't silently no-op.
_V3_JOB_DDL = schema.JOB_TABLE_DDL.replace("'enrich_t1_sync', ", "")
assert _V3_JOB_DDL != schema.JOB_TABLE_DDL


def _make_v3_db(path) -> None:
    """Recreate a v3 database: current schema minus document_enrichment,
    with the v3 job.kind vocabulary."""
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    with conn:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE document_enrichment")
        conn.execute("DROP TABLE job")
        conn.execute(_V3_JOB_DDL)
        conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        # v3-era data that must survive (job + its job_event child)
        conn.execute(
            "INSERT INTO job (kind, payload, status, created_at) VALUES"
            " ('poll_source', '{}', 'done', '2026-06-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO job_event (job_id, ts, type) VALUES"
            " (1, '2026-06-01T00:00:01Z', 'done')")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()


def test_migrate_v3_to_v4(tmp_path):
    db_path = tmp_path / "v3.db"
    _make_v3_db(db_path)

    conn = db_mod.connect(db_path)
    assert migrations.read_version(conn) == 3
    # the v3 vocab really was in force before migrating
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job (kind, status, created_at) VALUES"
            " ('enrich_t1_sync', 'queued', '2026-06-11T00:00:00Z')")
    conn.rollback()

    version = db_mod.init_db(conn)
    assert version == SCHEMA_VERSION == 7
    assert migrations.read_version(conn) == 7

    # the new table exists, byte-identical to fresh-create
    fresh = db_mod.connect(tmp_path / "fresh.db")
    db_mod.init_db(fresh)

    def ddl(c, name):
        return c.execute("SELECT sql FROM sqlite_master WHERE name=?",
                         (name,)).fetchone()[0]

    for table in ("document_enrichment", "job"):
        assert ddl(conn, table) == ddl(fresh, table)
    fresh.close()

    # old job row + its job_event child intact (rowids preserved)
    row = conn.execute("SELECT id, kind, status FROM job").fetchone()
    assert (row["id"], row["kind"], row["status"]) == (1, "poll_source",
                                                       "done")
    assert conn.execute("SELECT job_id FROM job_event").fetchone()[0] == 1
    # backup scratch tables are gone
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {t for t in tables if t.startswith("_mig_")}

    # the new vocab is live and the CHECK still rejects garbage
    with conn:
        conn.execute(
            "INSERT INTO job (kind, status, created_at) VALUES"
            " ('enrich_t1_sync', 'queued', '2026-06-11T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job (kind, status, created_at) VALUES"
            " ('bogus_kind', 'queued', '2026-06-11T00:00:00Z')")
    conn.rollback()

    # the new table is usable (PK = document_id; needs a document row)
    with conn:
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'text',"
            " 'body', 'h-v4-smoke')")
        doc_id = conn.execute("SELECT id FROM document WHERE"
                              " content_hash='h-v4-smoke'").fetchone()[0]
        conn.execute(
            "INSERT INTO document_enrichment (document_id, summary,"
            " event_type, model, prompt_version, created_at) VALUES"
            " (?, 's', 'other', 'm', 'v', '2026-06-11T00:00:00Z')", (doc_id,))
    conn.close()


# --- v4 -> v5 (office extraction: media_type vocab += xlsx/docx) --------------

# The v4 document CHECK vocabulary, derived from the current DDL by removing
# the v5 additions — guarded so a future vocab change can't silently no-op.
_V4_DOCUMENT_DDL = schema.DOCUMENT_TABLE_DDL.replace(", 'xlsx', 'docx')", ")")
assert _V4_DOCUMENT_DDL != schema.DOCUMENT_TABLE_DDL


def _make_v4_db(path) -> None:
    """Recreate a v4 database: the current schema with the v4 CHECK vocab on
    document.media_type (the v4 -> v5 delta)."""
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    with conn:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE document")  # drops its indexes + triggers
        conn.execute(_V4_DOCUMENT_DDL)
        for ddl in (*schema.DOCUMENT_INDEX_DDL,
                    *schema.DOCUMENT_FTS_TRIGGER_DDL):
            conn.execute(ddl)
        conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        # v4-era data that must survive (incl. a document_link child row and
        # a v3-vocab media_type)
        conn.execute(
            "INSERT INTO source (name, type, credibility_tier, created_at)"
            " VALUES ('RBI Notifications', 'rss', 1, '2026-06-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO document (source_id, url, title, fetched_at,"
            " media_type, content_text, content_hash)"
            " VALUES (1, 'https://rbi.org.in/x', 'Old circular',"
            " '2026-06-01T00:00:00Z', 'tweet', 'old body text', 'h-old')")
        conn.execute(
            "INSERT INTO document_link (document_id, url, created_at)"
            " VALUES (1, 'https://rbi.org.in/annex.xlsx',"
            " '2026-06-01T00:00:00Z')")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()


def test_migrate_v4_to_v5(tmp_path):
    db_path = tmp_path / "v4.db"
    _make_v4_db(db_path)

    conn = db_mod.connect(db_path)
    assert migrations.read_version(conn) == 4
    # the v4 vocab really was in force before migrating
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'xlsx', 'x',"
            " 'h-pre')")
    conn.rollback()  # the failed INSERT left an implicit transaction open

    version = db_mod.init_db(conn)
    assert version == SCHEMA_VERSION == 7
    assert migrations.read_version(conn) == 7

    # old data intact across the rebuild (rowids preserved, FK child intact)
    row = conn.execute(
        "SELECT title, content_text, source_id, media_type FROM document"
        " WHERE id=1").fetchone()
    assert (row["title"], row["content_text"], row["source_id"],
            row["media_type"]) == ("Old circular", "old body text", 1, "tweet")
    row = conn.execute(
        "SELECT url FROM document_link WHERE document_id=1").fetchone()
    assert row["url"] == "https://rbi.org.in/annex.xlsx"
    # FTS index survived the rebuild
    assert conn.execute("SELECT rowid FROM document_fts"
                        " WHERE document_fts MATCH 'circular'").fetchone()
    # backup scratch tables are gone
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {t for t in tables if t.startswith("_mig_")}

    # the new vocabularies are live...
    with conn:
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash, title) VALUES ('2026-06-11T00:00:00Z', 'xlsx',"
            " 'state-wise grant allocation figures', 'h-xlsx', 'Annex II')")
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'docx',"
            " 'committee report draft text', 'h-docx')")
    # ...the recreated FTS triggers index new rows...
    assert conn.execute("SELECT rowid FROM document_fts"
                        " WHERE document_fts MATCH 'allocation'").fetchone()
    # ...and the CHECK still rejects garbage
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES ('2026-06-11T00:00:00Z', 'xls', 'x',"
            " 'h-bad')")  # legacy .xls maps to 'xlsx'; 'xls' is NOT vocab
    conn.rollback()
    conn.close()


def test_migrated_v4_matches_fresh_schema(tmp_path):
    """A migrated v4 DB declares a byte-identical document table (and its
    indexes/triggers) to a fresh v5 DB — the rebuild executes the exact
    fresh-create DDL strings."""
    migrated = tmp_path / "m4.db"
    _make_v4_db(migrated)
    conn_m = db_mod.connect(migrated)
    db_mod.init_db(conn_m)

    conn_f = db_mod.connect(tmp_path / "f5.db")
    db_mod.init_db(conn_f)

    def schema_rows(conn):
        return conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name='document'"
            " AND sql IS NOT NULL ORDER BY name").fetchall()

    migrated_rows = [tuple(r) for r in schema_rows(conn_m)]
    fresh_rows = [tuple(r) for r in schema_rows(conn_f)]
    assert migrated_rows == fresh_rows
    assert any(name == "document" for name, _ in migrated_rows)
    conn_m.close()
    conn_f.close()


# --- v5 -> v6 (Phase 2: event_embedding + brief_item.payload) ------------------

# The v5 brief_item DDL, derived from the current DDL by removing the v6
# payload column — guarded so a future column change can't silently no-op.
_V5_BRIEF_ITEM_DDL = schema.BRIEF_ITEM_TABLE_DDL.replace(
    "    payload     TEXT NOT NULL DEFAULT '{}',\n", "")
assert _V5_BRIEF_ITEM_DDL != schema.BRIEF_ITEM_TABLE_DDL


def _make_v5_db(path) -> None:
    """Recreate a v5 database: current schema minus event_embedding, with
    the payload-less brief_item."""
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    with conn:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE event_embedding")
        conn.execute("DROP TABLE brief_item")  # drops its index too
        conn.execute(_V5_BRIEF_ITEM_DDL)
        conn.execute(schema.BRIEF_ITEM_INDEX_DDL[0])
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        # v5-era data that must survive
        conn.execute(
            "INSERT INTO brief (brief_date, generated_at) VALUES"
            " ('2026-06-01', '2026-06-01T06:00:00Z')")
        conn.execute(
            "INSERT INTO brief_item (brief_id, section, rank, object_type,"
            " object_id, reason_json, seen) VALUES"
            " (1, 'watch_dev', 1, 'document', 7, '{\"reason\":\"r\"}', 1)")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()


def test_migrate_v5_to_v6(tmp_path):
    db_path = tmp_path / "v5.db"
    _make_v5_db(db_path)

    conn = db_mod.connect(db_path)
    assert migrations.read_version(conn) == 5
    version = db_mod.init_db(conn)
    assert version == SCHEMA_VERSION == 7
    assert migrations.read_version(conn) == 7

    # new table + rebuilt table are byte-identical to fresh-create
    fresh = db_mod.connect(tmp_path / "fresh6.db")
    db_mod.init_db(fresh)

    def ddl(c, name):
        return c.execute("SELECT sql FROM sqlite_master WHERE name=?",
                         (name,)).fetchone()[0]

    for table in ("event_embedding", "brief_item"):
        assert ddl(conn, table) == ddl(fresh, table)
    fresh.close()

    # old brief_item row intact; payload took its default
    row = conn.execute("SELECT * FROM brief_item WHERE id=1").fetchone()
    assert (row["section"], row["rank"], row["object_type"], row["object_id"],
            row["seen"]) == ("watch_dev", 1, "document", 7, 1)
    assert row["reason_json"] == '{"reason":"r"}'
    assert row["payload"] == "{}"
    # the index was recreated and scratch tables are gone
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND tbl_name='brief_item'")}
    assert "idx_brief_item_brief" in indexes
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {t for t in tables if t.startswith("_mig_")}
    assert "event_embedding" in tables

    # both new structures are usable
    with conn:
        conn.execute(
            "INSERT INTO brief_item (brief_id, section, rank, object_type,"
            " object_id, payload) VALUES (1, 'thread_move', 1, 'thread', 3,"
            " '{\"title\":\"t\"}')")
        conn.execute(
            "INSERT INTO event (title, event_type, doc_count, created_at)"
            " VALUES ('E', 'other', 1, '2026-06-11T00:00:00Z')")
        event_id = conn.execute("SELECT id FROM event").fetchone()[0]
        conn.execute(
            "INSERT INTO event_embedding (event_id, model, dim, vector)"
            " VALUES (?, 'centroid', 2, x'0000803f00000040')", (event_id,))
    conn.close()
