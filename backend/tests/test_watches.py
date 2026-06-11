"""Deterministic watch matching: topic FTS watch + entity alias watch."""

from __future__ import annotations

import json

from connect.knowledge import watches as watch_logic
from connect.storage import watches as watch_dao
from connect.storage.pg import utc_now

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


async def _ingest(container, db, text, title):
    return (await container.pipeline.ingest_text(db, text,
                                                 title=title)).document


async def test_topic_watch_sets_watch_hit(container, db):
    watch = await watch_dao.insert(
        db, kind="topic", label="FPI disclosure",
        query_fts='"portfolio investors"')
    doc = await _ingest(container, db, DOC, "SEBI tightens FPI norms")

    cur = await db.execute(
        "SELECT watch_id, object_type, object_id FROM watch_hit")
    hits = [(r["watch_id"], r["object_type"], r["object_id"])
            for r in await cur.fetchall()]
    assert (watch.id, "document", doc.id) in hits
    cur = await db.execute(
        "SELECT watch_hit FROM document WHERE id=%s", (doc.id,))
    assert (await cur.fetchone())["watch_hit"] is True


async def test_entity_alias_watch_matches_word_boundary(container, db):
    await db.execute(
        "INSERT INTO entity (name, entity_type, aliases, created_at)"
        " VALUES (%s,%s,%s,%s)",
        ("Securities and Exchange Board of India", "organization",
         json.dumps(["SEBI"]), utc_now()))
    cur = await db.execute("SELECT id FROM entity")
    entity_id = (await cur.fetchone())["id"]
    watch = await watch_dao.insert(
        db, kind="entity", label="SEBI", entity_id=entity_id)

    doc = await _ingest(container, db, DOC, "SEBI tightens FPI norms")
    other = await _ingest(container, db, OTHER,
                          "Irrigation project approved")

    cur = await db.execute(
        "SELECT object_id FROM watch_hit WHERE watch_id=%s", (watch.id,))
    hit_ids = {r["object_id"] for r in await cur.fetchall()}
    assert doc.id in hit_ids
    assert other.id not in hit_ids
    assert other.watch_hit is False


async def test_non_matching_doc_gets_no_hits(container, db):
    await watch_dao.insert(db, kind="topic", label="crypto",
                           query_fts="cryptocurrency")
    doc = await _ingest(container, db, OTHER,
                        "Irrigation project approved")
    cur = await db.execute("SELECT COUNT(*) AS n FROM watch_hit")
    assert (await cur.fetchone())["n"] == 0
    assert doc.watch_hit is False


async def test_muted_watch_does_not_match(container, db):
    await watch_dao.insert(db, kind="topic", label="muted",
                           query_fts="irrigation", muted=True)
    await _ingest(container, db, OTHER, "Irrigation project approved")
    cur = await db.execute("SELECT COUNT(*) AS n FROM watch_hit")
    assert (await cur.fetchone())["n"] == 0


async def test_badges_and_seen_cursor(container, db):
    watch = await watch_dao.insert(
        db, kind="topic", label="FPI", query_fts='"portfolio investors"')
    await _ingest(container, db, DOC, "SEBI tightens FPI norms")

    assert await watch_logic.badges(db) == {watch.id: 1}
    await watch_logic.mark_seen(db, watch.id)
    assert await watch_logic.badges(db) == {watch.id: 0}
