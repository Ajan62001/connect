"""Deterministic watch matching: topic FTS watch + entity alias watch."""

from __future__ import annotations

import json

from connect.knowledge import watches as watch_logic
from connect.storage import watches as watch_dao
from connect.storage.db import utc_now

DOC = """
The Securities and Exchange Board of India tightened disclosure norms for
foreign portfolio investors, requiring granular ownership data from funds
holding more than fifty percent of their assets in a single corporate
group. SEBI said the framework addresses concerns about circumvention of
minimum public shareholding rules.
"""

OTHER = """
The state cabinet approved a new irrigation project in the Vidarbha region
covering four districts, with a budget outlay of three thousand crore.
"""


def _ingest(container, text, title):
    return container.pipeline.ingest_text(text, title=title).document


def test_topic_watch_sets_watch_hit(container):
    watch = watch_dao.insert(
        container.db, kind="topic", label="FPI disclosure",
        query_fts='"portfolio investors"')
    doc = _ingest(container, DOC, "SEBI tightens FPI norms")

    hits = container.db.execute(
        "SELECT watch_id, object_type, object_id FROM watch_hit").fetchall()
    assert (watch.id, "document", doc.id) in [tuple(r) for r in hits]
    flag = container.db.execute(
        "SELECT watch_hit FROM document WHERE id=?", (doc.id,)).fetchone()[0]
    assert flag == 1


def test_entity_alias_watch_matches_word_boundary(container):
    container.db.execute(
        "INSERT INTO entity (name, entity_type, aliases, created_at)"
        " VALUES (?,?,?,?)",
        ("Securities and Exchange Board of India", "organization",
         json.dumps(["SEBI"]), utc_now()))
    container.db.commit()
    entity_id = container.db.execute(
        "SELECT id FROM entity").fetchone()[0]
    watch = watch_dao.insert(
        container.db, kind="entity", label="SEBI", entity_id=entity_id)

    doc = _ingest(container, DOC, "SEBI tightens FPI norms")
    other = _ingest(container, OTHER, "Irrigation project approved")

    hit_ids = {r[0] for r in container.db.execute(
        "SELECT object_id FROM watch_hit WHERE watch_id=?", (watch.id,))}
    assert doc.id in hit_ids
    assert other.id not in hit_ids
    assert other.watch_hit is False


def test_non_matching_doc_gets_no_hits(container):
    watch_dao.insert(container.db, kind="topic", label="crypto",
                     query_fts="cryptocurrency")
    doc = _ingest(container, OTHER, "Irrigation project approved")
    count = container.db.execute(
        "SELECT COUNT(*) FROM watch_hit").fetchone()[0]
    assert count == 0
    assert doc.watch_hit is False


def test_muted_watch_does_not_match(container):
    watch_dao.insert(container.db, kind="topic", label="muted",
                     query_fts="irrigation", muted=True)
    _ingest(container, OTHER, "Irrigation project approved")
    count = container.db.execute(
        "SELECT COUNT(*) FROM watch_hit").fetchone()[0]
    assert count == 0


def test_badges_and_seen_cursor(container):
    watch = watch_dao.insert(
        container.db, kind="topic", label="FPI", query_fts='"portfolio investors"')
    _ingest(container, DOC, "SEBI tightens FPI norms")

    assert watch_logic.badges(container.db) == {watch.id: 1}
    watch_logic.mark_seen(container.db, watch.id)
    assert watch_logic.badges(container.db) == {watch.id: 0}
