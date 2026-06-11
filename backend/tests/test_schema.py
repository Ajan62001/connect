"""Schema creation + migration version round-trips."""

from __future__ import annotations

import pytest

from connect.storage import db as db_mod
from connect.storage import migrations
from connect.storage.schema import SCHEMA_VERSION, StorageVersionError


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_fresh_db_creates_full_schema(conn):
    version = db_mod.init_db(conn)
    assert version == SCHEMA_VERSION == 8

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "meta", "source", "document", "document_link", "document_topic",
        "claim", "evidence",
        "entity", "entity_mention", "event", "event_type", "event_assignment",
        "edge", "story", "claim_sighting", "verdict_history", "contradiction",
        "dossier", "dossier_section", "job", "job_event", "watch", "watch_hit",
        "brief", "brief_item", "view_cursor", "calendar_event", "llm_call",
        "source_stats", "document_embedding", "event_embedding",
        "question", "finding", "finding_evidence",
    }
    missing = expected - tables
    assert not missing, f"missing tables: {missing}"
    # FTS5 external-content tables exist
    assert "document_fts" in tables
    assert "claim_fts" in tables


def test_version_round_trip(conn):
    db_mod.init_db(conn)
    assert migrations.read_version(conn) == SCHEMA_VERSION
    # re-opening an existing DB is a no-op at the same version
    assert db_mod.init_db(conn) == SCHEMA_VERSION


def test_refuses_newer_db(conn):
    db_mod.init_db(conn)
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(StorageVersionError):
        db_mod.init_db(conn)


def test_equal_version_apply_is_noop(conn):
    db_mod.init_db(conn)
    assert migrations.apply(conn, SCHEMA_VERSION) == SCHEMA_VERSION


def test_fts_triggers_index_documents(conn):
    db_mod.init_db(conn)
    conn.execute(
        "INSERT INTO document (fetched_at, media_type, content_text,"
        " content_hash, title) VALUES ('2026-01-01T00:00:00Z', 'text',"
        " 'the digital rupee pilot expands', 'h1', 'RBI update')")
    conn.commit()
    row = conn.execute(
        "SELECT rowid FROM document_fts WHERE document_fts MATCH 'rupee'"
    ).fetchone()
    assert row is not None
