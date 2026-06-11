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
from connect.storage.pg import Jsonb, utc_now
from dbutil import q1, qall, qv, qvals

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


async def ingest_text(container, db, text, title="Doc"):
    return (await container.pipeline.ingest_text(db, text,
                                                 title=title)).document


async def insert_doc(conn, *, source_id=None, status="pending",
                     watch_hit=False, canonical=None,
                     text="some body text", hash_suffix=""):
    cur = await conn.execute(
        "INSERT INTO document (source_id, title, fetched_at, media_type,"
        " content_text, content_hash, enrichment_status,"
        " canonical_document_id, watch_hit)"
        " VALUES (%s,%s,%s, 'text', %s, %s, %s, %s, %s) RETURNING id",
        (source_id, "t", utc_now(), text,
         f"h-{hash_suffix or id(text)}-{status}-{watch_hit}",
         status, canonical, bool(watch_hit)))
    return int((await cur.fetchone())["id"])


async def insert_source(conn, name, *, t1_exempt=False, notes=None,
                        config=None):
    cur = await conn.execute(
        "INSERT INTO source (name, type, config, credibility_tier,"
        " t1_exempt, notes, created_at) VALUES (%s, 'manual', %s, 2, %s,"
        " %s, %s) RETURNING id",
        (name, Jsonb(config or {}), bool(t1_exempt), notes, utc_now()))
    return int((await cur.fetchone())["id"])


# --- prompt guards -----------------------------------------------------------


def test_t1_system_prompt_embeds_vocabularies():
    for topic in T1_TOPICS:
        assert topic in T1_SYSTEM
    for name in EVENT_TYPE_NAMES:
        assert name in T1_SYSTEM
    assert "appointment" in EVENT_TYPE_NAMES
    assert "mou_signing" in EVENT_TYPE_NAMES


# --- persistence -------------------------------------------------------------


async def test_persist_t1_writes_every_row_type(container, db):
    doc = await ingest_text(container, db, BODY, title="RBI hikes")
    conn = db

    stats = await persist.persist_t1(conn, document_id=doc.id,
                                     result=t1_result(),
                                     model="claude-haiku-4-5")
    assert stats["topics"] == 2
    assert stats["entities_created"] == 1
    assert stats["mentions"] == 1
    assert stats["claims"] == 1
    assert stats["claims_dropped_span"] == 0

    row = await q1(conn, "SELECT * FROM document_enrichment"
                         " WHERE document_id=%s", doc.id)
    assert row["event_type"] == "rbi_action"
    assert row["model"] == "claude-haiku-4-5"
    assert row["prompt_version"] == T1_PROMPT_VERSION

    topics = set(await qvals(
        conn, "SELECT topic FROM document_topic WHERE document_id=%s"
              " AND source='t1'", doc.id))
    assert topics == {"monetary-policy", "banking"}

    ent = await q1(conn, "SELECT * FROM entity")
    assert ent["name"] == "Reserve Bank of India"
    assert ent["entity_type"] == "organization"
    assert ent["grade"] == 1
    assert ent["aliases"] == ["Reserve Bank of India"]

    mention = await q1(conn, "SELECT * FROM entity_mention"
                             " WHERE document_id=%s", doc.id)
    assert mention["entity_id"] == ent["id"]
    assert mention["method"] == "llm"
    assert mention["surface"] == "Reserve Bank of India"
    assert mention["span_start"] is not None

    claim = await q1(conn, "SELECT * FROM claim")
    assert claim["check_worthiness"] == 0.9
    assert claim["first_document_id"] == doc.id
    sighting = await q1(conn, "SELECT * FROM claim_sighting"
                              " WHERE document_id=%s", doc.id)
    assert sighting["claim_id"] == claim["id"]
    assert sighting["stance"] == "asserts"
    assert sighting["grade"] == 1
    assert sighting["extractor_model"] == "claude-haiku-4-5"

    drow = await q1(conn, "SELECT enrichment_tier, enrichment_status"
                          " FROM document WHERE id=%s", doc.id)
    assert (drow["enrichment_tier"], drow["enrichment_status"]) == (1, "done")


async def test_persist_t1_is_idempotent(container, db):
    doc = await ingest_text(container, db, BODY)
    conn = db
    await persist.persist_t1(conn, document_id=doc.id, result=t1_result(),
                             model="m")
    await persist.persist_t1(conn, document_id=doc.id, result=t1_result(),
                             model="m")

    counts = {
        table: await qv(conn, f"SELECT COUNT(*) FROM {table}")
        for table in ("document_enrichment", "document_topic", "entity",
                      "entity_mention", "claim", "claim_sighting")}
    assert counts == {"document_enrichment": 1, "document_topic": 2,
                      "entity": 1, "entity_mention": 1, "claim": 1,
                      "claim_sighting": 1}


async def test_span_verification_drops_invented_quotes(container, db):
    doc = await ingest_text(container, db, BODY)
    conn = db
    result = t1_result(claims=(
        ("Real claim.", 0.95,
         "inflation remains above the tolerance band"),       # verbatim
        ("Invented claim.", 0.95,
         "the repo rate was slashed to 4 percent"),            # not in text
        ("Low-worthiness claim.", 0.3,
         "Analysts expect one more hike this year"),           # below 0.6
    ))
    stats = await persist.persist_t1(conn, document_id=doc.id,
                                     result=result, model="m")
    assert stats["claims"] == 1
    assert stats["claims_dropped_span"] == 1
    assert stats["claims_below_threshold"] == 1
    assert await qvals(conn, "SELECT text FROM claim") == ["Real claim."]


async def test_span_check_is_whitespace_normalized(container, db):
    body = "Rates   were\nraised to 6.75   percent today."
    doc = await ingest_text(container, db, body)
    result = t1_result(claims=(
        ("Claim.", 0.9, "raised to 6.75 percent"),))
    stats = await persist.persist_t1(db, document_id=doc.id,
                                     result=result, model="m")
    assert stats["claims"] == 1


async def test_entity_alias_exact_reuse_and_growth(container, db):
    conn = db
    doc1 = await ingest_text(container, db, BODY, title="one")
    doc2 = await ingest_text(container, db, BODY + " More words here.",
                             title="two")

    await persist.persist_t1(conn, document_id=doc1.id, result=t1_result(),
                             model="m")
    # same entity, different casing -> alias-exact match (normalized),
    # novel surface appended to aliases
    await persist.persist_t1(
        conn, document_id=doc2.id,
        result=t1_result(entities=(("RESERVE BANK OF INDIA", "organization"),)),
        model="m")

    rows = await qall(conn, "SELECT * FROM entity")
    assert len(rows) == 1
    aliases = rows[0]["aliases"]
    assert aliases == ["Reserve Bank of India", "RESERVE BANK OF INDIA"]
    entity_ids = set(await qvals(
        conn, "SELECT DISTINCT entity_id FROM entity_mention"))
    assert entity_ids == {rows[0]["id"]}

    # third sighting of a known alias: reuse, NO further alias growth
    doc3 = await ingest_text(container, db, BODY + " Even more words.",
                             title="three")
    await persist.persist_t1(
        conn, document_id=doc3.id,
        result=t1_result(entities=(("RESERVE BANK OF INDIA", "organization"),)),
        model="m")
    aliases = await qv(conn, "SELECT aliases FROM entity")
    assert len(aliases) == 2


async def test_unknown_vocab_degrades_to_other(container, db):
    doc = await ingest_text(container, db, BODY)
    result = t1_result(event_type="alien_invasion",
                       topics=("monetary-policy", "not-a-topic"),
                       entities=(("Mars Authority", "alien_org"),))
    await persist.persist_t1(db, document_id=doc.id, result=result,
                             model="m")
    conn = db
    assert await qv(conn, "SELECT event_type FROM document_enrichment") \
        == "other"
    assert await qvals(conn, "SELECT topic FROM document_topic") \
        == ["monetary-policy"]
    assert await qv(conn, "SELECT entity_type FROM entity") == "other"


# --- sweep selection -----------------------------------------------------------


async def test_sweep_selection_rules(container, db):
    conn = db
    normal = await insert_source(conn, "normal src")
    exempt = await insert_source(conn, "exempt src", t1_exempt=True)

    eligible_plain = await insert_doc(conn, source_id=normal,
                                      hash_suffix="a")
    sourceless = await insert_doc(conn, hash_suffix="b")
    dup = await insert_doc(conn, source_id=normal,
                           canonical=eligible_plain,
                           status="skipped_dup", hash_suffix="c")
    exempted = await insert_doc(conn, source_id=exempt, hash_suffix="d")
    exempt_watch_hit = await insert_doc(conn, source_id=exempt,
                                        watch_hit=True, hash_suffix="e")
    already_done = await insert_doc(conn, source_id=normal, status="done",
                                    hash_suffix="f")

    service = sweep.EnrichmentService(
        provider=None, batch_runner=None,
        governor=Governor(container.pool, 2.0))
    ids = {r["id"] for r in await service.select_eligible(conn)}
    assert eligible_plain in ids
    assert sourceless in ids
    assert exempt_watch_hit in ids          # watch_hit overrides t1_exempt
    assert dup not in ids
    assert exempted not in ids
    assert already_done not in ids


# --- sync sweep + governor -------------------------------------------------------


async def test_sync_sweep_enriches_and_ledgers(container, db):
    conn = db
    for i in range(3):
        await insert_doc(conn, text=BODY, hash_suffix=f"sync-{i}")
    provider = MockProvider(respond=t1_result())
    service = sweep.EnrichmentService(provider=provider,
                                      batch_runner=None,
                                      governor=Governor(container.pool,
                                                        2.0))
    stats = await service.run_sync(conn)
    assert stats["done"] == 3
    assert stats["failed"] == 0
    assert not stats["halted_budget"]
    assert len(provider.calls) == 3
    # ledger rows with computed cost (2200 in / 300 out on haiku = $0.0037)
    rows = await qall(conn, "SELECT model, cost_estimate, batch_id"
                            " FROM llm_call")
    assert len(rows) == 3
    assert all(r["model"] == "claude-haiku-4-5" for r in rows)
    assert all(abs(r["cost_estimate"] - 0.0037) < 1e-9 for r in rows)
    assert all(r["batch_id"] is None for r in rows)
    assert await qv(conn, "SELECT COUNT(*) FROM document"
                          " WHERE enrichment_status='done'") == 3


async def test_governor_halts_sync_sweep_midway(container, db):
    conn = db
    for i in range(3):
        await insert_doc(conn, text=BODY, hash_suffix=f"budget-{i}")
    per_call = estimated_t1_cost("claude-haiku-4-5", batch=False)
    provider = MockProvider(respond=t1_result())
    service = sweep.EnrichmentService(
        provider=provider, batch_runner=None,
        governor=Governor(container.pool,
                          per_call * 1.5))  # room for exactly one call
    stats = await service.run_sync(conn)
    assert stats["done"] == 1
    assert stats["halted_budget"] is True
    assert stats["remaining_pending"] == 2
    assert len(provider.calls) == 1
    statuses = await qvals(
        conn, "SELECT enrichment_status FROM document ORDER BY id")
    assert statuses == ["done", "pending", "pending"]


async def test_fast_path_enrich_document_respects_governor(container, db):
    conn = db
    doc = await ingest_text(container, db, BODY)
    provider = MockProvider(respond=t1_result())
    service = sweep.EnrichmentService(provider=provider,
                                      batch_runner=None,
                                      governor=Governor(container.pool,
                                                        0.0))
    result = await service.enrich_document(conn, doc.id)
    assert result.startswith("skipped: daily LLM budget exceeded")
    assert provider.calls == []
    assert await qv(conn, "SELECT enrichment_status FROM document"
                          " WHERE id=%s", doc.id) == "pending"

    service.governor = Governor(container.pool, 2.0)
    result = await service.enrich_document(conn, doc.id)
    assert result.startswith("done")
    assert await qv(conn, "SELECT enrichment_status FROM document"
                          " WHERE id=%s", doc.id) == "done"


# --- batch sweep -----------------------------------------------------------------


async def test_batch_sweep_round_trip(container, db):
    conn = db
    for i in range(2):
        await insert_doc(conn, text=BODY, hash_suffix=f"batch-{i}")
    runner = MockBatchRunner(respond=lambda item: t1_result())
    service = sweep.EnrichmentService(
        provider=MockProvider(respond=t1_result()),
        batch_runner=runner, governor=Governor(container.pool, 2.0),
        batch_poll_seconds=0.001)
    stats = await service.run_batch(conn)
    assert stats["submitted"] == 2
    assert stats["done"] == 2

    # the batch carried the EnrichmentT1 schema and the frozen system prompt
    items, schema = runner.submitted[0]
    assert schema is EnrichmentT1
    assert all(i.system == T1_SYSTEM for i in items)
    assert all(i.model == "claude-haiku-4-5" for i in items)

    # ledger rows are batch-priced (50% off) and carry the batch id
    rows = await qall(conn,
                      "SELECT cost_estimate, batch_id FROM llm_call")
    assert len(rows) == 2
    assert all(abs(r["cost_estimate"] - 0.00185) < 1e-9 for r in rows)
    assert all(r["batch_id"] == "mock-batch-1" for r in rows)

    assert await qv(conn,
                    "SELECT COUNT(*) FROM document_enrichment") == 2
    assert await qv(conn, "SELECT COUNT(*) FROM document"
                          " WHERE enrichment_status='done'") == 2


async def test_batch_submit_respects_governor_projection(container, db):
    conn = db
    for i in range(4):
        await insert_doc(conn, text=BODY, hash_suffix=f"proj-{i}")
    per_item = estimated_t1_cost("claude-haiku-4-5", batch=True)
    runner = MockBatchRunner(respond=lambda item: t1_result())
    service = sweep.EnrichmentService(
        provider=MockProvider(respond=t1_result()),
        batch_runner=runner,
        governor=Governor(container.pool,
                          per_item * 2))  # 4 docs projected > budget
    stats = await service.run_batch(conn)
    assert stats["submitted"] == 0
    assert stats["halted_budget"] is True
    assert runner.submitted == []  # never reached the API
    assert await qv(conn, "SELECT COUNT(*) FROM document"
                          " WHERE enrichment_status='pending'") == 4


async def test_batch_failed_items_marked_failed(container, db):
    conn = db
    await insert_doc(conn, text=BODY, hash_suffix="fail-0")
    runner = MockBatchRunner(respond=None)  # every item errors
    service = sweep.EnrichmentService(
        provider=MockProvider(respond=t1_result()),
        batch_runner=runner, governor=Governor(container.pool, 2.0),
        batch_poll_seconds=0.001)
    stats = await service.run_batch(conn)
    assert stats["failed"] == 1
    assert await qv(conn,
                    "SELECT enrichment_status FROM document") == "failed"


# --- fast-path hook ----------------------------------------------------------------


async def test_pipeline_fast_path_fires_on_watch_hit(container, db):
    conn = db
    await conn.execute(
        "INSERT INTO watch (kind, label, query_fts, created_at)"
        " VALUES ('topic', 'GST', 'GST', %s)", (utc_now(),))
    seen: list[tuple] = []

    async def hook(_conn, doc_id, source_id, watch_hit):
        seen.append((doc_id, source_id, watch_hit))

    container.pipeline.enrich_fast_path = hook

    doc = await ingest_text(container, db,
                            "GST collections rose 11 percent in May.")
    assert seen == [(doc.id, None, True)]

    # non-matching doc: hook called with watch_hit=False (the container
    # decides fact-checker fast-pathing from source_id)
    seen.clear()
    doc2 = await ingest_text(container, db,
                             "Unrelated cricket news item.")
    assert seen == [(doc2.id, None, False)]


async def test_is_fact_checker_source(db):
    conn = db
    by_notes = await insert_source(conn, "AltNews-like",
                                   notes="Fact-checker (verified live).")
    by_config = await insert_source(conn, "Configured",
                                    config={"fact_checker": True})
    plain = await insert_source(conn, "Plain news")
    assert await sweep.is_fact_checker_source(conn, by_notes)
    assert await sweep.is_fact_checker_source(conn, by_config)
    assert not await sweep.is_fact_checker_source(conn, plain)
    assert not await sweep.is_fact_checker_source(conn, None)


async def test_entity_watch_matches_real_aliases(container, db):
    """T0 watch matching uses the REAL entity alias table: an alias added by
    enrichment makes the watch fire on a later ingest."""
    conn = db
    doc = await ingest_text(container, db, BODY)
    await persist.persist_t1(conn, document_id=doc.id, result=t1_result(),
                             model="m")
    entity_id = await qv(conn, "SELECT id FROM entity")
    # grow an alias the canonical name would not match
    await conn.execute("UPDATE entity SET aliases=%s WHERE id=%s",
                       (Jsonb(["Reserve Bank of India", "RBI"]),
                        entity_id))
    await conn.execute(
        "INSERT INTO watch (kind, label, entity_id, created_at)"
        " VALUES ('entity', 'RBI watch', %s, %s)", (entity_id, utc_now()))

    hit_doc = await ingest_text(container, db,
                                "RBI announced a new framework today.")
    assert await qv(conn, "SELECT watch_hit FROM document WHERE id=%s",
                    hit_doc.id) is True
    hits = await qvals(conn, "SELECT object_id FROM watch_hit")
    assert hit_doc.id in set(hits)
