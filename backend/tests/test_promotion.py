"""T1 -> T2 promotion trigger rules: the pure table + the DB gatherer."""

from __future__ import annotations

import pytest
from kb_factories import add_t1, insert_doc, insert_source

from connect.knowledge.enrichment import promotion
from connect.storage.db import utc_now


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

def test_evaluate_unknown_document(container):
    assert promotion.evaluate(container.db, 99999) == []


def test_evaluate_watch_hit_and_official(container):
    conn = container.db
    tier1 = insert_source(conn, "PIB", tier=1)
    doc = insert_doc(conn, source_id=tier1, watch_hit=1)
    add_t1(conn, doc)
    assert promotion.evaluate(conn, doc) == ["watch_hit", "official_tier1"]


def test_evaluate_fact_checker_source(container):
    conn = container.db
    src = insert_source(conn, "Some Fact Check Feed", tier=2,
                        notes="fact-check outlet")
    doc = insert_doc(conn, source_id=src)
    add_t1(conn, doc)
    assert promotion.evaluate(conn, doc) == ["fact_checker"]


def test_evaluate_check_worthiness_threshold(container):
    conn = container.db
    doc = insert_doc(conn)
    add_t1(conn, doc)
    with conn:
        cur = conn.execute(
            "INSERT INTO claim (text, first_document_id, check_worthiness,"
            " created_at) VALUES ('c', ?, 0.72, ?)", (doc, utc_now()))
        conn.execute(
            "INSERT INTO claim_sighting (claim_id, document_id, stance,"
            " created_at) VALUES (?, ?, 'asserts', ?)",
            (cur.lastrowid, doc, utc_now()))
    assert promotion.evaluate(conn, doc) == ["check_worthiness"]


def test_evaluate_cluster_heat(container):
    conn = container.db
    now = utc_now()
    with conn:
        conn.execute("INSERT INTO event (title, event_type, doc_count,"
                     " created_at) VALUES ('E', 'other', 5, ?)", (now,))
    docs = [insert_doc(conn) for _ in range(5)]
    add_t1(conn, docs[0])
    with conn:
        for d in docs:
            conn.execute(
                "INSERT INTO event_assignment (document_id, event_id,"
                " method, created_at) VALUES (?, 1, 'attach', ?)", (d, now))
    assert "cluster_heat" in promotion.evaluate(conn, docs[0])

    # hits older than the 48h window do not count
    with conn:
        conn.execute("UPDATE event_assignment SET created_at ="
                     " '2026-01-01T00:00:00.000Z' WHERE document_id != ?",
                     (docs[0],))
    assert promotion.evaluate(conn, docs[0]) == []


def test_evaluate_manual_only(container):
    conn = container.db
    doc = insert_doc(conn)
    add_t1(conn, doc)
    assert promotion.evaluate(conn, doc) == []
    assert promotion.evaluate(conn, doc, manual=True) == ["manual"]
