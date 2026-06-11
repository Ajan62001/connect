"""Event clustering: attach / new / gray-zone adjudication paths with
synthetic embeddings, assignment audit rows, centroid running-mean math,
and the 3/10/25 summary-refresh schedule. MockProvider only."""

from __future__ import annotations

import json

import pytest
from dbutil import q1, qall, qv
from kb_factories import pad, t1_doc
from mock_llm import MockProvider

from connect.knowledge.linking import event_clusterer as ec
from connect.llm.spend import Governor

V_A = [1.0, 0.0, 0.0]
V_B = [0.0, 1.0, 0.0]


def governor(container, budget=2.0):
    return Governor(container.pool, budget)


async def seed_event(container, conn, *, event_type="rbi_action",
                     entities=("RBI", "Repo Rate"), vec=V_A,
                     title="RBI hikes repo rate"):
    """First doc -> new event (the clusterer's own create path)."""
    doc = await t1_doc(conn, title=title, event_type=event_type,
                       entities=entities, vec=vec)
    result = await ec.assign_document(conn, None, governor(container), doc)
    assert result.created_event
    return result.event_id


# --- pure math ----------------------------------------------------------------------

def test_score_weights():
    assert ec.score_candidate(cosine=1.0, jaccard=1.0,
                              same_event_type=True) == pytest.approx(1.0)
    assert ec.score_candidate(cosine=1.0, jaccard=0.0,
                              same_event_type=False) == pytest.approx(0.5)
    assert ec.score_candidate(cosine=0.0, jaccard=1.0,
                              same_event_type=True) == pytest.approx(0.5)


def test_centroid_running_mean():
    assert ec.update_centroid([1.0, 0.0], 1, [0.8, 0.6]) == \
        pytest.approx([0.9, 0.3])
    # n=3 members absorbing a 4th
    assert ec.update_centroid([0.5, 0.5], 3, [1.0, 1.0]) == \
        pytest.approx([0.625, 0.625])


def test_cosine_handles_missing_vectors():
    assert ec.cosine_similarity(None, V_A) == 0.0
    assert ec.cosine_similarity(V_A, [1.0, 0.0]) == 0.0  # dim mismatch
    assert ec.cosine_similarity(V_A, V_A) == pytest.approx(1.0)
    assert ec.cosine_similarity(V_A, [-1.0, 0.0, 0.0]) == 0.0  # clamped


# --- attach path (no LLM) -------------------------------------------------------------

async def test_attach_above_threshold(container, db):
    conn = db
    event_id = await seed_event(container, conn)
    doc2 = await t1_doc(conn, title="Repo rate up 25bps",
                        event_type="rbi_action",
                        entities=("RBI", "Repo Rate"), vec=V_A)
    result = await ec.assign_document(conn, None, governor(container), doc2)

    assert not result.created_event
    assert result.event_id == event_id
    assert result.method == "attach"
    assert result.score == pytest.approx(1.0)

    row = await q1(conn, "SELECT * FROM event WHERE id=%s", event_id)
    assert row["doc_count"] == 2
    # audit rows: one 'new' (seed) + one 'attach'
    audits = await qall(
        conn,
        "SELECT method, score, event_id FROM event_assignment ORDER BY id")
    assert [a["method"] for a in audits] == ["new", "attach"]
    assert audits[1]["event_id"] == event_id
    assert audits[1]["score"] == pytest.approx(1.0)


async def test_attach_updates_centroid_running_mean(container, db):
    conn = db
    event_id = await seed_event(container, conn, vec=[1.0, 0.0])
    doc2 = await t1_doc(conn, event_type="rbi_action",
                        entities=("RBI", "Repo Rate"), vec=[0.8, 0.6])
    result = await ec.assign_document(conn, None, governor(container), doc2)
    assert result.method == "attach"  # cosine .8 -> score .9
    assert await ec.get_event_centroid(conn, event_id) == \
        pytest.approx(pad([0.9, 0.3]))


# --- new-event path --------------------------------------------------------------------

async def test_new_event_below_threshold(container, db):
    conn = db
    first = await seed_event(container, conn)
    # orthogonal vector, different type, disjoint entities -> score 0.0
    doc2 = await t1_doc(conn, title="Court ruling on spectrum",
                        event_type="court_ruling",
                        entities=("Supreme Court",), vec=V_B)
    result = await ec.assign_document(conn, None, governor(container), doc2)
    assert result.created_event
    assert result.event_id != first
    assert result.method == "new"
    event = await q1(conn, "SELECT * FROM event WHERE id=%s",
                     result.event_id)
    assert event["title"] == "Court ruling on spectrum"
    assert event["event_type"] == "court_ruling"
    assert event["doc_count"] == 1
    # the new event's centroid is the doc vector
    assert await ec.get_event_centroid(conn, result.event_id) == \
        pytest.approx(pad(V_B))


async def test_event_outside_activity_window_not_candidate(container, db):
    conn = db
    event_id = await seed_event(container, conn)  # rbi_action window: 3 days
    await conn.execute("UPDATE event SET last_seen_at = '2026-01-01',"
                       " occurred_on = '2026-01-01' WHERE id=%s",
                       (event_id,))
    doc2 = await t1_doc(conn, event_type="rbi_action",
                        entities=("RBI", "Repo Rate"), vec=V_A)
    result = await ec.assign_document(conn, None, governor(container), doc2)
    assert result.created_event  # identical doc, but the window has closed


# --- gray zone --------------------------------------------------------------------------

async def gray_zone_doc(conn, *, title="RBI statement on liquidity"):
    """Same type + identical vector + disjoint entities -> 0.5 + 0.2 = 0.7,
    inside (0.55, 0.80)."""
    return await t1_doc(conn, title=title, event_type="rbi_action",
                        entities=("Liquidity Window",), vec=V_A)


async def test_gray_zone_adjudication_attach(container, db):
    conn = db
    event_id = await seed_event(container, conn)
    doc2 = await gray_zone_doc(conn)
    provider = MockProvider(respond_by_schema={
        ec.EventAdjudication: ec.EventAdjudication(choice=1)})
    result = await ec.assign_document(conn, provider, governor(container),
                                      doc2)

    assert result.method == "adjudicated"
    assert not result.created_event
    assert result.event_id == event_id
    # the adjudication call was FAST tier with the closed menu
    call = [c for c in provider.calls
            if c["schema"] is ec.EventAdjudication][0]
    assert "NEW EVENT" in call["user_text"]
    assert "RBI hikes repo rate" in call["user_text"]
    # audit row carries the full adjudication payload
    audit = await q1(
        conn,
        "SELECT adjudication FROM event_assignment WHERE document_id=%s",
        doc2)
    payload = json.loads(audit["adjudication"])
    assert payload["choice"] == 1
    assert payload["menu"][0]["event_id"] == event_id
    assert 0.55 < payload["menu"][0]["score"] < 0.80
    # adjudication cost was ledgered
    assert await qv(conn, "SELECT COUNT(*) FROM llm_call WHERE purpose="
                          "'event_adjudication'") == 1


async def test_gray_zone_adjudication_new(container, db):
    conn = db
    event_id = await seed_event(container, conn)
    doc2 = await gray_zone_doc(conn)
    provider = MockProvider(respond_by_schema={
        ec.EventAdjudication: ec.EventAdjudication(choice=0)})
    result = await ec.assign_document(conn, provider, governor(container),
                                      doc2)
    assert result.method == "adjudicated"
    assert result.created_event
    assert result.event_id != event_id


async def test_gray_zone_without_provider_degrades_to_new(container, db):
    conn = db
    await seed_event(container, conn)
    doc2 = await gray_zone_doc(conn)
    result = await ec.assign_document(conn, None, governor(container), doc2)
    assert result.method == "new"
    assert result.created_event
    payload = json.loads(await qv(
        conn,
        "SELECT adjudication FROM event_assignment WHERE document_id=%s",
        doc2))
    assert payload["fallback"] == "no_provider"


async def test_gray_zone_budget_exceeded_degrades_to_new(container, db):
    conn = db
    await seed_event(container, conn)
    doc2 = await gray_zone_doc(conn)
    provider = MockProvider(respond_by_schema={
        ec.EventAdjudication: ec.EventAdjudication(choice=1)})
    result = await ec.assign_document(conn, provider,
                                      governor(container, -1.0), doc2)
    assert result.method == "new"
    assert result.created_event
    assert provider.calls == []  # never reached the model
    payload = json.loads(await qv(
        conn,
        "SELECT adjudication FROM event_assignment WHERE document_id=%s",
        doc2))
    assert payload["fallback"] == "budget_exceeded"


# --- summary refresh schedule (3 / 10 / 25 only) -------------------------------------

async def test_summary_refresh_fires_at_log_schedule(container, db):
    conn = db
    provider = MockProvider(respond_by_schema={
        ec.EventSummary: ec.EventSummary(title="Canonical title",
                                         summary="Canonical summary.")})
    event_id = await seed_event(container, conn)  # size 1

    async def attach_one():
        return await t1_doc(conn, event_type="rbi_action",
                            entities=("RBI", "Repo Rate"), vec=V_A)

    def summary_calls():
        return len([c for c in provider.calls
                    if c["schema"] is ec.EventSummary])

    r2 = await ec.assign_document(conn, provider, governor(container),
                                  await attach_one())
    assert (r2.method, r2.summary_refreshed) == ("attach", False)
    assert summary_calls() == 0

    r3 = await ec.assign_document(conn, provider, governor(container),
                                  await attach_one())  # size 3 -> refresh
    assert (r3.method, r3.summary_refreshed) == ("attach", True)
    assert summary_calls() == 1
    event = await q1(conn,
                     "SELECT title, description FROM event WHERE id=%s",
                     event_id)
    assert event["title"] == "Canonical title"
    assert event["description"] == "Canonical summary."

    r4 = await ec.assign_document(conn, provider, governor(container),
                                  await attach_one())  # size 4 -> no refresh
    assert (r4.method, r4.summary_refreshed) == ("attach", False)
    assert summary_calls() == 1

    # jump to size 9, the next attach crosses 10 -> refresh
    await conn.execute("UPDATE event SET doc_count = 9 WHERE id=%s",
                       (event_id,))
    r10 = await ec.assign_document(conn, provider, governor(container),
                                   await attach_one())
    assert r10.summary_refreshed is True
    assert summary_calls() == 2
    # ledgered under its own purpose
    assert await qv(conn, "SELECT COUNT(*) FROM llm_call WHERE purpose="
                          "'event_summary'") == 2


async def test_not_t1_enriched_returns_none(container, db):
    conn = db
    from kb_factories import insert_doc
    doc = await insert_doc(conn)  # no document_enrichment row
    assert await ec.assign_document(conn, None, governor(container),
                                    doc) is None


# --- T2 wired into the sweep (promoted docs only) ---------------------------------

async def test_sync_sweep_runs_t2_for_promoted_docs(container, db):
    """A watch-hit doc coming through the T1 sweep gets clustered (T2);
    an unpromoted doc stays at tier 1 with no assignment."""
    from kb_factories import insert_doc
    from connect.knowledge.enrichment import sweep
    from connect.knowledge.enrichment.t1 import EnrichmentT1, T1Entity

    conn = db
    promoted = await insert_doc(conn, title="Watch-hit doc", watch_hit=True)
    plain = await insert_doc(conn, title="Plain doc")

    t1_out = EnrichmentT1(
        summary="Summary.", event_type="statement",
        entities=[T1Entity(surface="SEBI", type="organization")], claims=[])
    provider = MockProvider(respond=t1_out)
    service = sweep.EnrichmentService(provider=provider,
                                      batch_runner=None,
                                      governor=governor(container))
    stats = await service.run_sync(conn)
    assert stats["done"] == 2

    tiers = {r["id"]: r["enrichment_tier"] for r in await qall(
        conn, "SELECT id, enrichment_tier FROM document")}
    assert tiers[promoted] == 2
    assert tiers[plain] == 1
    assignments = await qall(
        conn, "SELECT document_id, method FROM event_assignment")
    assert [(a["document_id"], a["method"]) for a in assignments] == \
        [(promoted, "new")]


async def test_batch_sweep_runs_t2_for_promoted_docs(container, db):
    from kb_factories import insert_doc
    from mock_llm import MockBatchRunner
    from connect.knowledge.enrichment import sweep
    from connect.knowledge.enrichment.t1 import EnrichmentT1, T1Entity

    conn = db
    promoted = await insert_doc(conn, title="Watch-hit doc",
                                watch_hit=True)
    await insert_doc(conn, title="Plain doc")

    t1_out = EnrichmentT1(
        summary="Summary.", event_type="statement",
        entities=[T1Entity(surface="SEBI", type="organization")], claims=[])
    runner = MockBatchRunner(respond=lambda item: t1_out)
    service = sweep.EnrichmentService(
        provider=MockProvider(respond=t1_out), batch_runner=runner,
        governor=governor(container), batch_poll_seconds=0.001)
    stats = await service.run_batch(conn)
    assert stats["done"] == 2
    assert stats["promoted_t2"] == 1
    assert await qv(
        conn, "SELECT document_id FROM event_assignment") == promoted
