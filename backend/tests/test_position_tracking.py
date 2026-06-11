"""Leader views & position tracking (v9): statement persistence (verbatim
grounding, speaker-type filtering, idempotent replace), shift detection
(consistent vs reversed, off-menu rejection, duplicate-pair skip, governor
halt), evolution summary staleness + [[s<id>]] marker validation, the views
/ document / dismiss API surfaces, the brief position_shift section, and the
statements backfill selection. MockProvider only — no network."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from kb_factories import entity_id as make_entity
from kb_factories import insert_doc, insert_source
from mock_llm import MockProvider

from connect.api.main import create_app
from connect.knowledge.enrichment import persist, sweep
from connect.knowledge.enrichment.prompts import T1_PROMPT_VERSION, T1_SYSTEM
from connect.knowledge.enrichment.t1 import (
    EnrichmentT1,
    T1Entity,
    T1Statement,
)
from connect.knowledge.linking import position_tracker
from connect.knowledge.linking.position_tracker import (
    EvolutionSummaryOut,
    ShiftJudgment,
)
from connect.llm.spend import Governor
from connect.storage.db import utc_now

BODY = ('Finance Minister Arjun Mehta said the government "will not raise '
        "GST rates this financial year\". He told reporters in Delhi that "
        "the fiscal deficit target stays at 4.5 percent of GDP. Officials "
        "said further reforms remain under discussion.")

QUOTE = "will not raise GST rates this financial year"


def t1_result(*, statements=(), entities=(("Arjun Mehta", "person"),
                                          ("Delhi", "place"))):
    return EnrichmentT1(
        summary="FM rules out GST hikes this year.", event_type="other",
        topics=["taxation"],
        entities=[T1Entity(surface=s, type=t) for s, t in entities],
        claims=[],
        statements=[T1Statement(**st) for st in statements])


def statement(quote=QUOTE, speaker="Arjun Mehta", topics=("taxation",),
              position_summary="Opposes raising GST rates this year"):
    return {"speaker_surface": speaker, "quote": quote,
            "topics": list(topics), "position_summary": position_summary}


def add_statement(conn, *, doc_id, entity_id, quote,
                  topics=("taxation",), position_summary=None,
                  stated_at=None):
    with conn:
        cur = conn.execute(
            "INSERT INTO statement (document_id, entity_id, quote, topics,"
            " position_summary, stated_at, grade, extractor_model,"
            " prompt_version, created_at)"
            " VALUES (?,?,?,?,?,?, 1, 'm', ?, ?)",
            (doc_id, entity_id, quote, json.dumps(list(topics)),
             position_summary, stated_at or utc_now(), T1_PROMPT_VERSION,
             utc_now()))
    return int(cur.lastrowid)


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


def wait_for_job(conn, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute("SELECT status, error FROM job WHERE id=?",
                           (job_id,)).fetchone()
        if row and row["status"] in ("done", "failed", "cancelled"):
            return row
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


# --- prompt / contract guards -------------------------------------------------------


def test_prompt_version_bumped_and_statement_rules_embedded():
    assert T1_PROMPT_VERSION == "t1-v2"
    assert "statements" in T1_SYSTEM
    assert "VERBATIM" in T1_SYSTEM
    assert "political_party" in T1_SYSTEM


def test_t1_contract_clamps_statements():
    result = EnrichmentT1(
        summary="s", event_type="other",
        statements=[statement(topics=["taxation", "budget", "banking",
                                      "markets"],
                              position_summary="x" * 300)] * 8)
    assert len(result.statements) == 6
    assert len(result.statements[0].topics) == 3
    assert len(result.statements[0].position_summary) == 140
    # old results without the statements key still parse (backfill safety)
    old = EnrichmentT1.model_validate({"summary": "s", "event_type": "o"})
    assert old.statements == []


# --- persistence ----------------------------------------------------------------------


def test_statement_persistence_writes_rows(container):
    conn = container.db
    doc_id = insert_doc(conn, text=BODY,
                        published_at="2026-06-01T09:00:00Z")
    stats = persist.persist_t1(
        conn, document_id=doc_id,
        result=t1_result(statements=[statement(
            topics=("taxation", "not-a-topic"))]),
        model="claude-haiku-4-5")
    assert stats["statements"] == 1
    assert stats["statements_dropped_span"] == 0
    assert stats["statements_dropped_speaker"] == 0

    row = conn.execute("SELECT * FROM statement").fetchone()
    assert row["document_id"] == doc_id
    assert row["quote"] == QUOTE
    assert row["quote_start"] is not None
    assert json.loads(row["topics"]) == ["taxation"]  # vocab-filtered
    assert row["position_summary"] == "Opposes raising GST rates this year"
    assert row["stated_at"] == "2026-06-01T09:00:00Z"  # published_at wins
    assert row["grade"] == 1
    assert row["extractor_model"] == "claude-haiku-4-5"
    assert row["prompt_version"] == T1_PROMPT_VERSION
    speaker = conn.execute("SELECT name, entity_type FROM entity"
                           " WHERE id=?", (row["entity_id"],)).fetchone()
    assert (speaker["name"], speaker["entity_type"]) == ("Arjun Mehta",
                                                         "person")


def test_statement_stated_at_falls_back_to_fetched_at(container):
    conn = container.db
    doc_id = insert_doc(conn, text=BODY)  # no published_at
    persist.persist_t1(conn, document_id=doc_id,
                       result=t1_result(statements=[statement()]), model="m")
    row = conn.execute("SELECT stated_at FROM statement").fetchone()
    fetched = conn.execute("SELECT fetched_at FROM document WHERE id=?",
                           (doc_id,)).fetchone()[0]
    assert row["stated_at"] == fetched


def test_statement_span_verification_drops_invented_quotes(container):
    conn = container.db
    doc_id = insert_doc(conn, text=BODY)
    stats = persist.persist_t1(
        conn, document_id=doc_id,
        result=t1_result(statements=[
            statement(),                                   # verbatim
            statement(quote="GST will rise sharply soon"),  # invented
        ]), model="m")
    assert stats["statements"] == 1
    assert stats["statements_dropped_span"] == 1
    assert conn.execute("SELECT COUNT(*) FROM statement").fetchone()[0] == 1


def test_statement_speaker_type_filtering(container):
    conn = container.db
    doc_id = insert_doc(conn, text=BODY)
    stats = persist.persist_t1(
        conn, document_id=doc_id,
        result=t1_result(statements=[
            statement(),                       # person -> kept
            statement(speaker="Delhi"),        # place -> dropped
            statement(speaker="Unknown Guy"),  # unresolvable -> dropped
        ]), model="m")
    assert stats["statements"] == 1
    assert stats["statements_dropped_speaker"] == 2
    rows = conn.execute(
        "SELECT e.entity_type FROM statement s"
        " JOIN entity e ON e.id = s.entity_id").fetchall()
    assert [r[0] for r in rows] == ["person"]


def test_statement_persistence_is_idempotent(container):
    conn = container.db
    doc_id = insert_doc(conn, text=BODY)
    for _ in range(2):
        persist.persist_t1(conn, document_id=doc_id,
                           result=t1_result(statements=[statement()]),
                           model="m")
    assert conn.execute("SELECT COUNT(*) FROM statement").fetchone()[0] == 1


def test_reenrichment_cascades_stale_shifts(container):
    """Re-persisting a doc replaces its statements; shifts built on the
    replaced rows die via FK cascade — never a dangling quote pair."""
    conn = container.db
    speaker = make_entity(conn, "Arjun Mehta", "person")
    old_doc = insert_doc(conn, text="old", published_at="2026-06-01")
    s1 = add_statement(conn, doc_id=old_doc, entity_id=speaker, quote="A")
    new_doc = insert_doc(conn, text=BODY, published_at="2026-06-10")
    s2 = add_statement(conn, doc_id=new_doc, entity_id=speaker, quote="B")
    with conn:
        conn.execute(
            "INSERT INTO position_shift (entity_id, topic,"
            " from_statement_id, to_statement_id, kind, detected_at)"
            " VALUES (?, 'taxation', ?, ?, 'reversed', ?)",
            (speaker, s1, s2, utc_now()))
    persist.persist_t1(conn, document_id=new_doc,
                       result=t1_result(statements=[statement()]), model="m")
    assert conn.execute("SELECT COUNT(*) FROM position_shift"
                        ).fetchone()[0] == 0


# --- shift detection ------------------------------------------------------------------


def seed_pair(conn, *, prior_quote="GST rates must come down",
              prior_summary="Wants GST rates cut",
              topic="taxation"):
    """One speaker with a prior statement and a new doc whose statement was
    just persisted — the detect_shifts input shape."""
    speaker = make_entity(conn, "Arjun Mehta", "person")
    prior_doc = insert_doc(conn, text=prior_quote,
                           published_at="2026-06-01T00:00:00Z")
    prior = add_statement(conn, doc_id=prior_doc, entity_id=speaker,
                          quote=prior_quote, topics=(topic,),
                          position_summary=prior_summary,
                          stated_at="2026-06-01T00:00:00Z")
    new_doc = insert_doc(conn, text=BODY,
                         published_at="2026-06-10T00:00:00Z")
    new = add_statement(conn, doc_id=new_doc, entity_id=speaker,
                        quote=QUOTE, topics=(topic,),
                        position_summary="Opposes raising GST rates",
                        stated_at="2026-06-10T00:00:00Z")
    return speaker, prior, new_doc, new


async def test_consistent_relation_creates_no_shift(container):
    conn = container.db
    _, prior, new_doc, _ = seed_pair(conn)
    provider = MockProvider(respond=ShiftJudgment(
        relation="consistent", versus_statement_id=prior))
    stats = await position_tracker.detect_shifts(
        conn, provider, Governor(conn, 2.0), new_doc)
    assert stats["pairs"] == 1
    assert stats["calls"] == 1
    assert stats["shifts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM position_shift"
                        ).fetchone()[0] == 0
    # the call carried the closed menu and was ledgered
    assert str(prior) in provider.calls[0]["user_text"]
    assert conn.execute("SELECT purpose FROM llm_call"
                        ).fetchone()[0] == "enrichment"


async def test_reversed_relation_creates_grounded_shift(container):
    conn = container.db
    speaker, prior, new_doc, new = seed_pair(conn)
    provider = MockProvider(respond=ShiftJudgment(
        relation="reversed", versus_statement_id=prior,
        note="Now rules out cuts after demanding them"))
    stats = await position_tracker.detect_shifts(
        conn, provider, Governor(conn, 2.0), new_doc)
    assert stats["shifts"] == 1
    row = conn.execute("SELECT * FROM position_shift").fetchone()
    assert (row["entity_id"], row["topic"]) == (speaker, "taxation")
    assert (row["from_statement_id"], row["to_statement_id"]) == (prior, new)
    assert row["kind"] == "reversed"
    assert row["note"] == "Now rules out cuts after demanding them"
    assert row["status"] == "open"


async def test_off_menu_versus_id_is_rejected(container):
    conn = container.db
    _, prior, new_doc, _ = seed_pair(conn)
    provider = MockProvider(respond=ShiftJudgment(
        relation="reversed", versus_statement_id=999_999, note="x"))
    stats = await position_tracker.detect_shifts(
        conn, provider, Governor(conn, 2.0), new_doc)
    assert stats["rejected_versus"] == 1
    assert stats["shifts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM position_shift"
                        ).fetchone()[0] == 0


async def test_duplicate_pair_is_skipped(container):
    conn = container.db
    _, prior, new_doc, _ = seed_pair(conn)
    provider = MockProvider(respond=ShiftJudgment(
        relation="reversed", versus_statement_id=prior, note="x"))
    governor = Governor(conn, 2.0)
    await position_tracker.detect_shifts(conn, provider, governor, new_doc)
    stats = await position_tracker.detect_shifts(
        conn, provider, governor, new_doc)
    assert stats["skipped_duplicate"] == 1
    assert stats["shifts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM position_shift"
                        ).fetchone()[0] == 1


async def test_no_prior_statement_means_no_call(container):
    conn = container.db
    speaker = make_entity(conn, "Arjun Mehta", "person")
    doc = insert_doc(conn, text=BODY)
    add_statement(conn, doc_id=doc, entity_id=speaker, quote=QUOTE)
    provider = MockProvider(respond=ShiftJudgment(
        relation="reversed", versus_statement_id=1))
    stats = await position_tracker.detect_shifts(
        conn, provider, Governor(conn, 2.0), doc)
    assert stats["pairs"] == 0
    assert provider.calls == []


async def test_governor_halts_shift_detection(container):
    conn = container.db
    _, _, new_doc, _ = seed_pair(conn)
    provider = MockProvider(respond=ShiftJudgment(
        relation="reversed", versus_statement_id=1))
    stats = await position_tracker.detect_shifts(
        conn, provider, Governor(conn, 0.0), new_doc)
    assert stats["halted_budget"] is True
    assert provider.calls == []


async def test_sweep_runs_shift_detection_after_persist(container):
    """The sync sweep wires detect_shifts after persist_t1: a doc whose
    statement reverses a prior one produces a position_shift row."""
    conn = container.db
    speaker = make_entity(conn, "Arjun Mehta", "person")
    prior_doc = insert_doc(conn, text="GST rates must come down",
                           published_at="2026-06-01T00:00:00Z",
                           )
    prior = add_statement(conn, doc_id=prior_doc, entity_id=speaker,
                          quote="GST rates must come down",
                          stated_at="2026-06-01T00:00:00Z")
    with conn:  # only the NEW doc is sweep-eligible
        conn.execute("UPDATE document SET enrichment_status='done'"
                     " WHERE id=?", (prior_doc,))
        cur = conn.execute(
            "INSERT INTO document (title, published_at, fetched_at,"
            " media_type, content_text, content_hash, enrichment_status)"
            " VALUES ('t', '2026-06-10T00:00:00Z', ?, 'text', ?,"
            " 'h-sweep-shift', 'pending')", (utc_now(), BODY))
        new_doc = int(cur.lastrowid)
    provider = MockProvider(respond_by_schema={
        EnrichmentT1: t1_result(statements=[statement()]),
        ShiftJudgment: ShiftJudgment(relation="reversed",
                                     versus_statement_id=prior,
                                     note="reversed on GST"),
    })
    service = sweep.EnrichmentService(conn, provider=provider,
                                      batch_runner=None,
                                      governor=Governor(conn, 2.0))
    stats = await service.run_sync()
    assert stats["done"] == 1
    row = conn.execute("SELECT * FROM position_shift").fetchone()
    assert row is not None
    assert row["from_statement_id"] == prior
    assert conn.execute("SELECT to_statement_id FROM position_shift"
                        ).fetchone()[0] == conn.execute(
        "SELECT id FROM statement WHERE document_id=?",
        (new_doc,)).fetchone()[0]


# --- evolution summaries ---------------------------------------------------------------


async def test_view_summary_lazy_generation_and_cache(container):
    conn = container.db
    speaker, prior, _, new = seed_pair(conn)
    provider = MockProvider(respond=EvolutionSummaryOut(
        text=f"Demanded cuts [[s{prior}]] then ruled them out [[s{new}]]."))
    governor = Governor(conn, 2.0)

    summary = await position_tracker.get_view_summary(
        conn, provider, governor, speaker, "taxation")
    assert summary is not None
    assert summary.stale is False
    assert summary.citations == [prior, new]
    assert f"[[s{prior}]]" in summary.text
    assert len(provider.calls) == 1
    row = conn.execute("SELECT * FROM view_summary").fetchone()
    assert (row["entity_id"], row["topic"]) == (speaker, "taxation")
    assert row["statement_count_at_gen"] == 2

    # fresh cache: a second read is $0 (no new call)
    again = await position_tracker.get_view_summary(
        conn, provider, governor, speaker, "taxation")
    assert again == summary
    assert len(provider.calls) == 1

    # +1 statement: still fresh (growth < 2)
    doc = insert_doc(conn, text="x1")
    add_statement(conn, doc_id=doc, entity_id=speaker, quote="x1",
                  stated_at="2026-06-11T00:00:00Z")
    await position_tracker.get_view_summary(
        conn, provider, governor, speaker, "taxation")
    assert len(provider.calls) == 1

    # +2 statements: stale -> regenerated
    doc = insert_doc(conn, text="x2")
    add_statement(conn, doc_id=doc, entity_id=speaker, quote="x2",
                  stated_at="2026-06-12T00:00:00Z")
    regenerated = await position_tracker.get_view_summary(
        conn, provider, governor, speaker, "taxation")
    assert len(provider.calls) == 2
    assert regenerated.stale is False

    # a shift detected after generation makes it stale again
    with conn:
        conn.execute(
            "INSERT INTO position_shift (entity_id, topic,"
            " from_statement_id, to_statement_id, kind, detected_at)"
            " VALUES (?, 'taxation', ?, ?, 'reversed', ?)",
            (speaker, prior, new, "2999-01-01T00:00:00Z"))
    assert position_tracker.is_stale(conn, speaker, "taxation")
    await position_tracker.get_view_summary(
        conn, provider, governor, speaker, "taxation")
    assert len(provider.calls) == 3


async def test_view_summary_strips_off_menu_markers(container):
    conn = container.db
    speaker, prior, _, new = seed_pair(conn)
    provider = MockProvider(respond=EvolutionSummaryOut(
        text=f"Real [[s{prior}]] and invented [[s999999]] citations."))
    summary = await position_tracker.get_view_summary(
        conn, provider, Governor(conn, 2.0), speaker, "taxation")
    assert summary.citations == [prior]
    assert "[[s999999]]" not in summary.text
    assert f"[[s{prior}]]" in summary.text


async def test_view_summary_expands_compound_markers(container):
    """Live-observed model output: several ids fused into ONE bracket
    ("[[s1,s2]]"). The ids must be kept (expanded into single markers), and
    any other malformed [[...]] token must be stripped."""
    conn = container.db
    speaker, prior, _, new = seed_pair(conn)
    provider = MockProvider(respond=EvolutionSummaryOut(
        text=f"Early support [[s{prior}, s{new}]] then doubt [[s{new}]]"
             " and junk [[see above]] [[s]]."))
    summary = await position_tracker.get_view_summary(
        conn, provider, Governor(conn, 2.0), speaker, "taxation")
    assert summary.citations == [prior, new]
    assert f"[[s{prior}]][[s{new}]]" in summary.text
    assert f"[[s{prior}, s{new}]]" not in summary.text
    assert "[[see above]]" not in summary.text
    assert "[[s]]" not in summary.text


async def test_view_summary_governor_block_serves_stale(container):
    conn = container.db
    speaker, prior, _, new = seed_pair(conn)
    provider = MockProvider(respond=EvolutionSummaryOut(text="cached"))
    await position_tracker.get_view_summary(
        conn, provider, Governor(conn, 2.0), speaker, "taxation")
    # two more statements -> stale; a $0 governor blocks regeneration
    for i in range(2):
        doc = insert_doc(conn, text=f"y{i}")
        add_statement(conn, doc_id=doc, entity_id=speaker, quote=f"y{i}")
    blocked = await position_tracker.get_view_summary(
        conn, provider, Governor(conn, 0.0), speaker, "taxation")
    assert blocked.stale is True
    assert blocked.text == "cached"
    assert len(provider.calls) == 1
    # no provider at all behaves the same
    no_provider = await position_tracker.get_view_summary(
        conn, None, Governor(conn, 2.0), speaker, "taxation")
    assert no_provider.stale is True


async def test_view_summary_none_without_statements(container):
    conn = container.db
    speaker = make_entity(conn, "Silent Person", "person")
    out = await position_tracker.get_view_summary(
        conn, MockProvider(respond=EvolutionSummaryOut(text="x")),
        Governor(conn, 2.0), speaker, "taxation")
    assert out is None


# --- API surfaces -------------------------------------------------------------------


def seed_views(conn):
    """One speaker, two topics, three statements, one open shift."""
    src = insert_source(conn, "PIB", tier=1)
    speaker = make_entity(conn, "Arjun Mehta", "person")
    d1 = insert_doc(conn, title="GST presser", text="cut GST now",
                    source_id=src, published_at="2026-06-01T00:00:00Z")
    s1 = add_statement(conn, doc_id=d1, entity_id=speaker,
                       quote="cut GST now", stated_at="2026-06-01T00:00:00Z",
                       position_summary="Wants GST cuts")
    d2 = insert_doc(conn, title="Budget speech", text=BODY, source_id=src,
                    published_at="2026-06-10T00:00:00Z")
    s2 = add_statement(conn, doc_id=d2, entity_id=speaker, quote=QUOTE,
                       stated_at="2026-06-10T00:00:00Z",
                       position_summary="Opposes raising GST rates")
    add_statement(conn, doc_id=d2, entity_id=speaker,
                  quote="the fiscal deficit target stays at 4.5 percent",
                  topics=("budget",), stated_at="2026-06-10T00:00:00Z",
                  position_summary="Committed to 4.5% deficit target")
    with conn:
        conn.execute(
            "INSERT INTO position_shift (entity_id, topic,"
            " from_statement_id, to_statement_id, kind, note, detected_at)"
            " VALUES (?, 'taxation', ?, ?, 'reversed', 'flip', ?)",
            (speaker, s1, s2, utc_now()))
    return speaker, (s1, s2), (d1, d2)


def test_entity_detail_has_views_flag(env):
    client, container = env
    speaker, _, _ = seed_views(container.db)
    silent = make_entity(container.db, "Quiet Org", "organization")
    assert client.get(f"/api/entities/{speaker}").json()["has_views"] is True
    assert client.get(f"/api/entities/{silent}").json()["has_views"] is False


def test_entity_views_index_endpoint(env):
    client, container = env
    speaker, _, _ = seed_views(container.db)
    res = client.get(f"/api/entities/{speaker}/views")
    assert res.status_code == 200
    body = res.json()
    assert body["entity"] == {"id": speaker, "name": "Arjun Mehta",
                              "entity_type": "person"}
    assert [t["topic"] for t in body["topics"]] == ["taxation", "budget"]
    taxation = body["topics"][0]
    assert taxation["statement_count"] == 2
    assert taxation["shift_count"] == 1
    assert taxation["first_at"] == "2026-06-01T00:00:00Z"
    assert taxation["last_at"] == "2026-06-10T00:00:00Z"
    assert taxation["latest_position"] == "Opposes raising GST rates"
    assert body["topics"][1]["shift_count"] == 0
    # 404 for a missing entity
    assert client.get("/api/entities/999999/views").status_code == 404


def test_topic_views_endpoint_with_lazy_summary(env):
    client, container = env
    conn = container.db
    speaker, (s1, s2), (d1, d2) = seed_views(conn)

    # no provider (backend/.env may carry a real key — force it out: tests
    # never talk to the network) -> summary null (nothing cached), but
    # statements + shifts still serve
    container.llm = None
    res = client.get(f"/api/entities/{speaker}/views/taxation")
    assert res.status_code == 200
    body = res.json()
    assert body["topic"] == "taxation"
    assert body["evolution_summary"] is None
    assert [s["id"] for s in body["statements"]] == [s2, s1]  # newest first
    first = body["statements"][0]
    assert first["quote"] == QUOTE
    assert first["document_id"] == d2
    assert first["source_name"] == "PIB"
    assert first["credibility_tier"] == 1
    assert first["title"] == "Budget speech"
    assert [s["id"] for s in (body["shifts"])] == [
        conn.execute("SELECT id FROM position_shift").fetchone()[0]]
    shift = body["shifts"][0]
    assert (shift["kind"], shift["note"]) == ("reversed", "flip")
    assert (shift["from_statement_id"], shift["to_statement_id"]) == (s1, s2)

    # inject a provider: the stale read triggers ONE regeneration, after
    # which reads serve the cache
    container.llm = MockProvider(respond=EvolutionSummaryOut(
        text=f"Reversed on GST [[s{s2}]]."))
    body = client.get(f"/api/entities/{speaker}/views/taxation").json()
    assert body["evolution_summary"]["text"] == f"Reversed on GST [[s{s2}]]."
    assert body["evolution_summary"]["citations"] == [s2]
    assert body["evolution_summary"]["stale"] is False
    assert len(container.llm.calls) == 1
    client.get(f"/api/entities/{speaker}/views/taxation")
    assert len(container.llm.calls) == 1  # cache hit: $0


def test_document_detail_lists_statements(env):
    client, container = env
    speaker, (s1, s2), (d1, d2) = seed_views(container.db)
    body = client.get(f"/api/documents/{d2}").json()
    assert len(body["statements"]) == 2
    st = body["statements"][0]
    assert st["id"] == s2
    assert st["speaker"] == {"id": speaker, "name": "Arjun Mehta",
                             "entity_type": "person"}
    assert st["quote"] == QUOTE
    assert st["topics"] == ["taxation"]
    assert st["position_summary"] == "Opposes raising GST rates"
    # a statement-less document serves an empty list
    assert client.get(f"/api/documents/{d1}").json()["statements"] != []
    bare = insert_doc(container.db, text="nothing said")
    assert client.get(f"/api/documents/{bare}").json()["statements"] == []


def test_dismiss_position_shift(env):
    client, container = env
    speaker, _, _ = seed_views(container.db)
    shift_id = container.db.execute(
        "SELECT id FROM position_shift").fetchone()[0]
    res = client.post(f"/api/position-shifts/{shift_id}/dismiss")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == shift_id
    assert body["status"] == "dismissed"
    assert body["entity_id"] == speaker
    assert body["topic"] == "taxation"
    # dismissed shifts leave the views surfaces
    views = client.get(f"/api/entities/{speaker}/views").json()
    assert views["topics"][0]["shift_count"] == 0
    topic = client.get(f"/api/entities/{speaker}/views/taxation").json()
    assert topic["shifts"] == []
    # unknown id -> 404
    assert client.post("/api/position-shifts/999999/dismiss"
                       ).status_code == 404


# --- brief integration ----------------------------------------------------------------


def test_brief_position_shift_section(env):
    client, container = env
    conn = container.db
    speaker, (s1, s2), _ = seed_views(conn)
    # a second, WATCHED speaker — must rank first
    watched = make_entity(conn, "Devika Rao", "person")
    d = insert_doc(conn, text="z1", published_at="2026-06-02T00:00:00Z")
    w1 = add_statement(conn, doc_id=d, entity_id=watched, quote="z1",
                       topics=("banking",),
                       stated_at="2026-06-02T00:00:00Z")
    d = insert_doc(conn, text="z2", published_at="2026-06-09T00:00:00Z")
    w2 = add_statement(conn, doc_id=d, entity_id=watched, quote="z2",
                       topics=("banking",),
                       stated_at="2026-06-09T00:00:00Z")
    with conn:
        conn.execute(
            "INSERT INTO position_shift (entity_id, topic,"
            " from_statement_id, to_statement_id, kind, detected_at)"
            " VALUES (?, 'banking', ?, ?, 'shifted', ?)",
            (watched, w1, w2, utc_now()))
        conn.execute(
            "INSERT INTO watch (kind, label, entity_id, created_at)"
            " VALUES ('entity', 'Rao watch', ?, ?)", (watched, utc_now()))
        # a dismissed shift never reaches the brief
        conn.execute(
            "INSERT INTO position_shift (entity_id, topic,"
            " from_statement_id, to_statement_id, kind, detected_at,"
            " status) VALUES (?, 'banking', ?, ?, 'shifted', ?,"
            " 'dismissed')", (watched, w1, w2, utc_now()))

    body = client.get("/api/brief/today").json()
    items = body["sections"]["position_shift"]
    assert len(items) == 2
    assert all(i["object_type"] == "position_shift" for i in items)
    # watched entity first
    assert items[0]["payload"]["entity_name"] == "Devika Rao"
    assert items[0]["payload"]["kind"] == "shifted"
    assert items[0]["reason"] == ("Shifted position on banking"
                                  " · was 2026-06-02, now 2026-06-09")
    second = items[1]
    assert second["payload"] == {
        "entity_id": speaker, "entity_name": "Arjun Mehta",
        "topic": "taxation", "kind": "reversed",
        "from_quote": "cut GST now", "to_quote": QUOTE,
        "from_date": "2026-06-01T00:00:00Z",
        "to_date": "2026-06-10T00:00:00Z"}
    assert second["reason"] == ("Reversed position on taxation"
                                " · was 2026-06-01, now 2026-06-10")


def test_brief_position_shift_only_since_last_brief(env):
    client, container = env
    conn = container.db
    # generate today's brief FIRST (becomes "the last brief" for tomorrow)
    assert client.get("/api/brief/today").json()[
        "sections"]["position_shift"] == []
    speaker, (s1, s2), _ = seed_views(conn)  # detected_at = now > generated_at
    # today's brief is frozen; a NEW day picks up shifts since the last one
    tomorrow = conn.execute("SELECT date('now', '+1 day')").fetchone()[0]
    from connect.knowledge import briefing
    briefing._generate(conn, tomorrow)
    rows = conn.execute(
        "SELECT bi.section, COUNT(*) AS n FROM brief_item bi"
        " JOIN brief b ON b.id = bi.brief_id WHERE b.brief_date = ?"
        " AND bi.section = 'position_shift' GROUP BY bi.section",
        (tomorrow,)).fetchone()
    assert rows["n"] == 1


# --- backfill -------------------------------------------------------------------------


def seed_backfill_corpus(conn):
    """Two docs enriched under t1-v1 (no statements) + one already on the
    current prompt + one never enriched."""
    old_ids = []
    for i in range(2):
        doc = insert_doc(conn, text=BODY, published_at="2026-06-01")
        with conn:
            conn.execute(
                "INSERT INTO document_enrichment (document_id, summary,"
                " event_type, model, prompt_version, created_at)"
                " VALUES (?, 's', 'other', 'm', 't1-v1', ?)",
                (doc, utc_now()))
            conn.execute("UPDATE document SET enrichment_status='done',"
                         " enrichment_tier=1 WHERE id=?", (doc,))
        old_ids.append(doc)
    current = insert_doc(conn, text=BODY)
    with conn:
        conn.execute(
            "INSERT INTO document_enrichment (document_id, summary,"
            " event_type, model, prompt_version, created_at)"
            " VALUES (?, 's', 'other', 'm', ?, ?)",
            (current, T1_PROMPT_VERSION, utc_now()))
        conn.execute("UPDATE document SET enrichment_status='done',"
                     " enrichment_tier=1 WHERE id=?", (current,))
    pending = insert_doc(conn, text=BODY)  # stays 'pending', NOT a target
    return old_ids, current, pending


def test_backfill_selection_filters_on_prompt_version(container):
    conn = container.db
    old_ids, current, pending = seed_backfill_corpus(conn)
    service = sweep.EnrichmentService(conn, provider=None, batch_runner=None,
                                      governor=Governor(conn, 2.0))
    ids = [r["id"] for r in service.select_eligible(target="statements")]
    assert ids == old_ids
    # the default selection is untouched (pending docs only)
    default_ids = {r["id"] for r in service.select_eligible()}
    assert pending in default_ids
    assert not set(old_ids) & default_ids


async def test_backfill_run_reextracts_and_converges(container):
    conn = container.db
    old_ids, _, _ = seed_backfill_corpus(conn)
    provider = MockProvider(respond_by_schema={
        EnrichmentT1: t1_result(statements=[statement()]),
        # the second doc's statement has a prior -> shift detection runs
        ShiftJudgment: ShiftJudgment(relation="consistent",
                                     versus_statement_id=1)})
    service = sweep.EnrichmentService(conn, provider=provider,
                                      batch_runner=None,
                                      governor=Governor(conn, 2.0))
    stats = await service.run_sync(target="statements")
    assert stats["done"] == 2
    for doc in old_ids:
        assert conn.execute(
            "SELECT prompt_version FROM document_enrichment"
            " WHERE document_id=?", (doc,)).fetchone()[0] == T1_PROMPT_VERSION
        assert conn.execute(
            "SELECT COUNT(*) FROM statement WHERE document_id=?",
            (doc,)).fetchone()[0] == 1
    # converged: nothing left to backfill
    assert service.select_eligible(target="statements") == []


def test_sweep_endpoint_accepts_statements_target(env):
    client, container = env
    conn = container.db
    old_ids, _, _ = seed_backfill_corpus(conn)
    container.enrichment.provider = MockProvider(respond_by_schema={
        EnrichmentT1: t1_result(statements=[statement()]),
        ShiftJudgment: ShiftJudgment(relation="consistent",
                                     versus_statement_id=1)})
    res = client.post("/api/enrichment/sweep",
                      json={"mode": "sync", "target": "statements"})
    assert res.status_code == 202
    job_id = res.json()["job_id"]
    row = wait_for_job(conn, job_id)
    assert row["status"] == "done", row["error"]
    payload = json.loads(conn.execute(
        "SELECT payload FROM job WHERE id=?", (job_id,)).fetchone()[0])
    assert payload["target"] == "statements"
    assert conn.execute("SELECT COUNT(*) FROM statement"
                        ).fetchone()[0] == len(old_ids)
    # garbage target rejected by the contract
    assert client.post("/api/enrichment/sweep",
                       json={"mode": "sync", "target": "bogus"}
                       ).status_code == 422
