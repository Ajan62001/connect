"""T1 -> T2 promotion trigger rules: the pure table + the DB gatherer."""

from __future__ import annotations

import pytest
from kb_factories import add_t1, insert_doc, insert_source

from connect.knowledge.enrichment import promotion
from connect.storage.pg import utc_now


# --- pure rules, table-driven ---------------------------------------------------

@pytest.mark.parametrize("kwargs, expected", [
    ({}, []),
    ({"watch_hit": True}, ["watch_hit"]),
    ({"fact_checker": True}, ["fact_checker"]),
    ({"official_tier1": True}, ["official_tier1"]),
    ({"max_check_worthiness": 0.7}, ["check_worthiness"]),
    ({"max_check_worthiness": 0.69}, []),
    ({"max_check_worthiness": None}, []),
    ({"cluster_docs_48h": 5}, ["cluster_heat"]),
    ({"cluster_docs_48h": 4}, []),
    ({"manual": True}, ["manual"]),
    # multiple triggers fire together, in stable order
    ({"watch_hit": True, "official_tier1": True,
      "max_check_worthiness": 0.9, "manual": True},
     ["watch_hit", "official_tier1", "check_worthiness", "manual"]),
])
def test_promotion_trigger_table(kwargs, expected):
    assert promotion.promotion_triggers(**kwargs) == expected


# --- DB gatherer ------------------------------------------------------------------

async def test_evaluate_unknown_document(db):
    assert await promotion.evaluate(db, 99999) == []


async def test_evaluate_watch_hit_and_official(db):
    conn = db
    tier1 = await insert_source(conn, "PIB", tier=1)
    doc = await insert_doc(conn, source_id=tier1, watch_hit=True)
    await add_t1(conn, doc)
    assert await promotion.evaluate(conn, doc) == \
        ["watch_hit", "official_tier1"]


async def test_evaluate_fact_checker_source(db):
    conn = db
    src = await insert_source(conn, "Some Fact Check Feed", tier=2,
                              notes="fact-check outlet")
    doc = await insert_doc(conn, source_id=src)
    await add_t1(conn, doc)
    assert await promotion.evaluate(conn, doc) == ["fact_checker"]


async def test_evaluate_check_worthiness_threshold(db):
    conn = db
    doc = await insert_doc(conn)
    await add_t1(conn, doc)
    cur = await conn.execute(
        "INSERT INTO claim (text, first_document_id, check_worthiness,"
        " created_at) VALUES ('c', %s, 0.72, %s) RETURNING id",
        (doc, utc_now()))
    claim_id = (await cur.fetchone())["id"]
    await conn.execute(
        "INSERT INTO claim_sighting (claim_id, document_id, stance,"
        " created_at) VALUES (%s, %s, 'asserts', %s)",
        (claim_id, doc, utc_now()))
    assert await promotion.evaluate(conn, doc) == ["check_worthiness"]


async def test_evaluate_cluster_heat(db):
    conn = db
    now = utc_now()
    cur = await conn.execute(
        "INSERT INTO event (title, event_type, doc_count, created_at)"
        " VALUES ('E', 'other', 5, %s) RETURNING id", (now,))
    event_id = (await cur.fetchone())["id"]
    docs = [await insert_doc(conn) for _ in range(5)]
    await add_t1(conn, docs[0])
    for d in docs:
        await conn.execute(
            "INSERT INTO event_assignment (document_id, event_id,"
            " method, created_at) VALUES (%s, %s, 'attach', %s)",
            (d, event_id, now))
    assert "cluster_heat" in await promotion.evaluate(conn, docs[0])

    # hits older than the 48h window do not count
    await conn.execute("UPDATE event_assignment SET created_at ="
                       " '2026-01-01T00:00:00.000Z' WHERE document_id != %s",
                       (docs[0],))
    assert await promotion.evaluate(conn, docs[0]) == []


async def test_evaluate_manual_only(db):
    conn = db
    doc = await insert_doc(conn)
    await add_t1(conn, doc)
    assert await promotion.evaluate(conn, doc) == []
    assert await promotion.evaluate(conn, doc, manual=True) == ["manual"]
