"""T1 enrichment: persistence (all row types, idempotent), grounding
discipline (verbatim spans), alias-exact entity reuse + alias growth,
governor behavior on sync and batch sweeps, sweep selection rules, the
fast-path hook. MockProvider/MockBatchRunner only — no network."""

from __future__ import annotations

import json

import pytest
from mock_llm import MockBatchRunner, MockProvider

from connect.knowledge.enrichment import persist, sweep
from connect.knowledge.enrichment.prompts import (
    T1_PROMPT_VERSION,
    T1_SYSTEM,
)
from connect.knowledge.enrichment.t1 import EnrichmentT1, T1Claim, T1Entity
from connect.knowledge.taxonomy import EVENT_TYPE_NAMES
from connect.domain.enums import T1_TOPICS
from connect.llm.spend import Governor, estimated_t1_cost
from connect.storage.db import utc_now

BODY = ("The Reserve Bank of India raised the repo rate by 25 basis points "
        "to 6.75 percent on Friday. Governor statements said inflation "
        "remains above the tolerance band. Analysts expect one more hike "
        "this year.")


def t1_result(*, summary="RBI raises repo rate by 25 bps to 6.75%.",
              event_type="rbi_action",
              topics=("monetary-policy", "banking"),
              entities=(("Reserve Bank of India", "organization"),),
              claims=(("RBI raised the repo rate to 6.75 percent.", 0.9,
                       "raised the repo rate by 25 basis points to 6.75 "
                       "percent"),)) -> EnrichmentT1:
    return EnrichmentT1(
        summary=summary, event_type=event_type, topics=list(topics),
        entities=[T1Entity(surface=s, type=t) for s, t in entities],
        claims=[T1Claim(text=t, check_worthiness=w, quoted_span=q)
                for t, w, q in claims])


def ingest_text(container, text, title="Doc"):
    return container.pipeline.ingest_text(text, title=title).document


def insert_doc(conn, *, source_id=None, status="pending", watch_hit=0,
               canonical=None, text="some body text", hash_suffix=""):
    with conn:
        cur = conn.execute(
            "INSERT INTO document (source_id, title, fetched_at, media_type,"
            " content_text, content_hash, enrichment_status,"
            " canonical_document_id, watch_hit)"
            " VALUES (?,?,?, 'text', ?, ?, ?, ?, ?)",
            (source_id, "t", utc_now(), text,
             f"h-{hash_suffix or id(text)}-{status}-{watch_hit}",
             status, canonical, watch_hit))
    return int(cur.lastrowid)


def insert_source(conn, name, *, t1_exempt=0, notes=None, config="{}"):
    with conn:
        cur = conn.execute(
            "INSERT INTO source (name, type, config, credibility_tier,"
            " t1_exempt, notes, created_at) VALUES (?, 'manual', ?, 2, ?,"
            " ?, ?)", (name, config, t1_exempt, notes, utc_now()))
    return int(cur.lastrowid)


# --- prompt guards -----------------------------------------------------------


def test_t1_system_prompt_embeds_vocabularies():
    for topic in T1_TOPICS:
        assert topic in T1_SYSTEM
    for name in EVENT_TYPE_NAMES:
        assert name in T1_SYSTEM
    assert "appointment" in EVENT_TYPE_NAMES
    assert "mou_signing" in EVENT_TYPE_NAMES


# --- persistence -------------------------------------------------------------


def test_persist_t1_writes_every_row_type(container):
    doc = ingest_text(container, BODY, title="RBI hikes")
    conn = container.db

    stats = persist.persist_t1(conn, document_id=doc.id, result=t1_result(),
                               model="claude-haiku-4-5")
    assert stats["topics"] == 2
    assert stats["entities_created"] == 1
    assert stats["mentions"] == 1
    assert stats["claims"] == 1
    assert stats["claims_dropped_span"] == 0

    row = conn.execute("SELECT * FROM document_enrichment"
                       " WHERE document_id=?", (doc.id,)).fetchone()
    assert row["event_type"] == "rbi_action"
    assert row["model"] == "claude-haiku-4-5"
    assert row["prompt_version"] == T1_PROMPT_VERSION

    topics = {r[0] for r in conn.execute(
        "SELECT topic FROM document_topic WHERE document_id=?"
        " AND source='t1'", (doc.id,))}
    assert topics == {"monetary-policy", "banking"}

    ent = conn.execute("SELECT * FROM entity").fetchone()
    assert ent["name"] == "Reserve Bank of India"
    assert ent["entity_type"] == "organization"
    assert ent["grade"] == 1
    assert json.loads(ent["aliases"]) == ["Reserve Bank of India"]

    mention = conn.execute("SELECT * FROM entity_mention"
                           " WHERE document_id=?", (doc.id,)).fetchone()
    assert mention["entity_id"] == ent["id"]
    assert mention["method"] == "llm"
    assert mention["surface"] == "Reserve Bank of India"
    assert mention["span_start"] is not None

    claim = conn.execute("SELECT * FROM claim").fetchone()
    assert claim["check_worthiness"] == 0.9
    assert claim["first_document_id"] == doc.id
    sighting = conn.execute("SELECT * FROM claim_sighting"
                            " WHERE document_id=?", (doc.id,)).fetchone()
    assert sighting["claim_id"] == claim["id"]
    assert sighting["stance"] == "asserts"
    assert sighting["grade"] == 1
    assert sighting["extractor_model"] == "claude-haiku-4-5"

    drow = conn.execute("SELECT enrichment_tier, enrichment_status"
                        " FROM document WHERE id=?", (doc.id,)).fetchone()
    assert (drow["enrichment_tier"], drow["enrichment_status"]) == (1, "done")


def test_persist_t1_is_idempotent(container):
    doc = ingest_text(container, BODY)
    conn = container.db
    persist.persist_t1(conn, document_id=doc.id, result=t1_result(),
                       model="m")
    persist.persist_t1(conn, document_id=doc.id, result=t1_result(),
                       model="m")

    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("document_enrichment", "document_topic", "entity",
                      "entity_mention", "claim", "claim_sighting")}
    assert counts == {"document_enrichment": 1, "document_topic": 2,
                      "entity": 1, "entity_mention": 1, "claim": 1,
                      "claim_sighting": 1}


def test_span_verification_drops_invented_quotes(container):
    doc = ingest_text(container, BODY)
    conn = container.db
    result = t1_result(claims=(
        ("Real claim.", 0.95,
         "inflation remains above the tolerance band"),       # verbatim
        ("Invented claim.", 0.95,
         "the repo rate was slashed to 4 percent"),            # not in text
        ("Low-worthiness claim.", 0.3,
         "Analysts expect one more hike this year"),           # below 0.6
    ))
    stats = persist.persist_t1(conn, document_id=doc.id, result=result,
                               model="m")
    assert stats["claims"] == 1
    assert stats["claims_dropped_span"] == 1
    assert stats["claims_below_threshold"] == 1
    texts = [r[0] for r in conn.execute("SELECT text FROM claim")]
    assert texts == ["Real claim."]


def test_span_check_is_whitespace_normalized(container):
    body = "Rates   were\nraised to 6.75   percent today."
    doc = ingest_text(container, body)
    result = t1_result(claims=(
        ("Claim.", 0.9, "raised to 6.75 percent"),))
    stats = persist.persist_t1(container.db, document_id=doc.id,
                               result=result, model="m")
    assert stats["claims"] == 1


def test_entity_alias_exact_reuse_and_growth(container):
    conn = container.db
    doc1 = ingest_text(container, BODY, title="one")
    doc2 = ingest_text(container, BODY + " More words here.", title="two")

    persist.persist_t1(conn, document_id=doc1.id, result=t1_result(),
                       model="m")
    # same entity, different casing -> alias-exact match (normalized),
    # novel surface appended to aliases
    persist.persist_t1(
        conn, document_id=doc2.id,
        result=t1_result(entities=(("RESERVE BANK OF INDIA", "organization"),)),
        model="m")

    rows = conn.execute("SELECT * FROM entity").fetchall()
    assert len(rows) == 1
    aliases = json.loads(rows[0]["aliases"])
    assert aliases == ["Reserve Bank of India", "RESERVE BANK OF INDIA"]
    entity_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM entity_mention")}
    assert entity_ids == {rows[0]["id"]}

    # third sighting of a known alias: reuse, NO further alias growth
    doc3 = ingest_text(container, BODY + " Even more words.", title="three")
    persist.persist_t1(
        conn, document_id=doc3.id,
        result=t1_result(entities=(("RESERVE BANK OF INDIA", "organization"),)),
        model="m")
    aliases = json.loads(conn.execute(
        "SELECT aliases FROM entity").fetchone()[0])
    assert len(aliases) == 2


def test_unknown_vocab_degrades_to_other(container):
    doc = ingest_text(container, BODY)
    result = t1_result(event_type="alien_invasion",
                       topics=("monetary-policy", "not-a-topic"),
                       entities=(("Mars Authority", "alien_org"),))
    persist.persist_t1(container.db, document_id=doc.id, result=result,
                       model="m")
    conn = container.db
    assert conn.execute("SELECT event_type FROM document_enrichment"
                        ).fetchone()[0] == "other"
    topics = [r[0] for r in conn.execute(
        "SELECT topic FROM document_topic")]
    assert topics == ["monetary-policy"]
    assert conn.execute("SELECT entity_type FROM entity"
                        ).fetchone()[0] == "other"


# --- sweep selection -----------------------------------------------------------


def test_sweep_selection_rules(container):
    conn = container.db
    normal = insert_source(conn, "normal src")
    exempt = insert_source(conn, "exempt src", t1_exempt=1)

    eligible_plain = insert_doc(conn, source_id=normal, hash_suffix="a")
    sourceless = insert_doc(conn, hash_suffix="b")
    dup = insert_doc(conn, source_id=normal, canonical=eligible_plain,
                     status="skipped_dup", hash_suffix="c")
    exempted = insert_doc(conn, source_id=exempt, hash_suffix="d")
    exempt_watch_hit = insert_doc(conn, source_id=exempt, watch_hit=1,
                                  hash_suffix="e")
    already_done = insert_doc(conn, source_id=normal, status="done",
                              hash_suffix="f")

    service = sweep.EnrichmentService(
        conn, provider=None, batch_runner=None,
        governor=Governor(conn, 2.0))
    ids = {r["id"] for r in service.select_eligible()}
    assert eligible_plain in ids
    assert sourceless in ids
    assert exempt_watch_hit in ids          # watch_hit overrides t1_exempt
    assert dup not in ids
    assert exempted not in ids
    assert already_done not in ids


# --- sync sweep + governor -------------------------------------------------------


async def test_sync_sweep_enriches_and_ledgers(container):
    conn = container.db
    for i in range(3):
        insert_doc(conn, text=BODY, hash_suffix=f"sync-{i}")
    provider = MockProvider(respond=t1_result())
    service = sweep.EnrichmentService(conn, provider=provider,
                                      batch_runner=None,
                                      governor=Governor(conn, 2.0))
    stats = await service.run_sync()
    assert stats["done"] == 3
    assert stats["failed"] == 0
    assert not stats["halted_budget"]
    assert len(provider.calls) == 3
    # ledger rows with computed cost (2200 in / 300 out on haiku = $0.0037)
    rows = conn.execute("SELECT model, cost_estimate, batch_id"
                        " FROM llm_call").fetchall()
    assert len(rows) == 3
    assert all(r["model"] == "claude-haiku-4-5" for r in rows)
    assert all(abs(r["cost_estimate"] - 0.0037) < 1e-9 for r in rows)
    assert all(r["batch_id"] is None for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM document"
                        " WHERE enrichment_status='done'").fetchone()[0] == 3


async def test_governor_halts_sync_sweep_midway(container):
    conn = container.db
    for i in range(3):
        insert_doc(conn, text=BODY, hash_suffix=f"budget-{i}")
    per_call = estimated_t1_cost("claude-haiku-4-5", batch=False)
    provider = MockProvider(respond=t1_result())
    service = sweep.EnrichmentService(
        conn, provider=provider, batch_runner=None,
        governor=Governor(conn, per_call * 1.5))  # room for exactly one call
    stats = await service.run_sync()
    assert stats["done"] == 1
    assert stats["halted_budget"] is True
    assert stats["remaining_pending"] == 2
    assert len(provider.calls) == 1
    statuses = [r[0] for r in conn.execute(
        "SELECT enrichment_status FROM document ORDER BY id")]
    assert statuses == ["done", "pending", "pending"]


async def test_fast_path_enrich_document_respects_governor(container):
    conn = container.db
    doc = ingest_text(container, BODY)
    provider = MockProvider(respond=t1_result())
    service = sweep.EnrichmentService(conn, provider=provider,
                                      batch_runner=None,
                                      governor=Governor(conn, 0.0))
    result = await service.enrich_document(doc.id)
    assert result.startswith("skipped: daily LLM budget exceeded")
    assert provider.calls == []
    assert conn.execute("SELECT enrichment_status FROM document WHERE id=?",
                        (doc.id,)).fetchone()[0] == "pending"

    service.governor = Governor(conn, 2.0)
    result = await service.enrich_document(doc.id)
    assert result.startswith("done")
    assert conn.execute("SELECT enrichment_status FROM document WHERE id=?",
                        (doc.id,)).fetchone()[0] == "done"


# --- batch sweep -----------------------------------------------------------------


async def test_batch_sweep_round_trip(container):
    conn = container.db
    for i in range(2):
        insert_doc(conn, text=BODY, hash_suffix=f"batch-{i}")
    runner = MockBatchRunner(respond=lambda item: t1_result())
    service = sweep.EnrichmentService(
        conn, provider=MockProvider(respond=t1_result()),
        batch_runner=runner, governor=Governor(conn, 2.0),
        batch_poll_seconds=0.001)
    stats = await service.run_batch()
    assert stats["submitted"] == 2
    assert stats["done"] == 2

    # the batch carried the EnrichmentT1 schema and the frozen system prompt
    items, schema = runner.submitted[0]
    assert schema is EnrichmentT1
    assert all(i.system == T1_SYSTEM for i in items)
    assert all(i.model == "claude-haiku-4-5" for i in items)

    # ledger rows are batch-priced (50% off) and carry the batch id
    rows = conn.execute(
        "SELECT cost_estimate, batch_id FROM llm_call").fetchall()
    assert len(rows) == 2
    assert all(abs(r["cost_estimate"] - 0.00185) < 1e-9 for r in rows)
    assert all(r["batch_id"] == "mock-batch-1" for r in rows)

    assert conn.execute("SELECT COUNT(*) FROM document_enrichment"
                        ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM document"
                        " WHERE enrichment_status='done'").fetchone()[0] == 2


async def test_batch_submit_respects_governor_projection(container):
    conn = container.db
    for i in range(4):
        insert_doc(conn, text=BODY, hash_suffix=f"proj-{i}")
    per_item = estimated_t1_cost("claude-haiku-4-5", batch=True)
    runner = MockBatchRunner(respond=lambda item: t1_result())
    service = sweep.EnrichmentService(
        conn, provider=MockProvider(respond=t1_result()),
        batch_runner=runner,
        governor=Governor(conn, per_item * 2))  # 4 docs projected > budget
    stats = await service.run_batch()
    assert stats["submitted"] == 0
    assert stats["halted_budget"] is True
    assert runner.submitted == []  # never reached the API
    assert conn.execute("SELECT COUNT(*) FROM document"
                        " WHERE enrichment_status='pending'"
                        ).fetchone()[0] == 4


async def test_batch_failed_items_marked_failed(container):
    conn = container.db
    insert_doc(conn, text=BODY, hash_suffix="fail-0")
    runner = MockBatchRunner(respond=None)  # every item errors
    service = sweep.EnrichmentService(
        conn, provider=MockProvider(respond=t1_result()),
        batch_runner=runner, governor=Governor(conn, 2.0),
        batch_poll_seconds=0.001)
    stats = await service.run_batch()
    assert stats["failed"] == 1
    assert conn.execute("SELECT enrichment_status FROM document"
                        ).fetchone()[0] == "failed"


# --- fast-path hook ----------------------------------------------------------------


def test_pipeline_fast_path_fires_on_watch_hit(container):
    conn = container.db
    with conn:
        conn.execute("INSERT INTO watch (kind, label, query_fts, created_at)"
                     " VALUES ('topic', 'GST', 'GST', ?)", (utc_now(),))
    seen: list[tuple] = []
    container.pipeline.enrich_fast_path = (
        lambda doc_id, source_id, watch_hit:
        seen.append((doc_id, source_id, watch_hit)))

    doc = ingest_text(container, "GST collections rose 11 percent in May.")
    assert seen == [(doc.id, None, True)]

    # non-matching doc: hook called with watch_hit=False (the container
    # decides fact-checker fast-pathing from source_id)
    seen.clear()
    doc2 = ingest_text(container, "Unrelated cricket news item.")
    assert seen == [(doc2.id, None, False)]


def test_is_fact_checker_source(container):
    conn = container.db
    by_notes = insert_source(conn, "AltNews-like",
                             notes="Fact-checker (verified live).")
    by_config = insert_source(conn, "Configured",
                              config=json.dumps({"fact_checker": True}))
    plain = insert_source(conn, "Plain news")
    assert sweep.is_fact_checker_source(conn, by_notes)
    assert sweep.is_fact_checker_source(conn, by_config)
    assert not sweep.is_fact_checker_source(conn, plain)
    assert not sweep.is_fact_checker_source(conn, None)


def test_entity_watch_matches_real_aliases(container):
    """T0 watch matching uses the REAL entity alias table: an alias added by
    enrichment makes the watch fire on a later ingest."""
    conn = container.db
    doc = ingest_text(container, BODY)
    persist.persist_t1(conn, document_id=doc.id, result=t1_result(),
                       model="m")
    entity_id = conn.execute("SELECT id FROM entity").fetchone()[0]
    # grow an alias the canonical name would not match
    with conn:
        conn.execute("UPDATE entity SET aliases=? WHERE id=?",
                     (json.dumps(["Reserve Bank of India", "RBI"]),
                      entity_id))
        conn.execute(
            "INSERT INTO watch (kind, label, entity_id, created_at)"
            " VALUES ('entity', 'RBI watch', ?, ?)", (entity_id, utc_now()))

    hit_doc = ingest_text(container, "RBI announced a new framework today.")
    row = conn.execute("SELECT watch_hit FROM document WHERE id=?",
                       (hit_doc.id,)).fetchone()
    assert row["watch_hit"] == 1
    hits = conn.execute("SELECT object_id FROM watch_hit").fetchall()
    assert hit_doc.id in {r[0] for r in hits}
