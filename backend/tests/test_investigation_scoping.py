"""Scope-stage pure math: §3.2b reaction-candidate pairs on synthetic
events + embeddings, the §3.2a causal-marker pre-scan, RRF fusion, the
coverage heuristic and seed resolution."""

from __future__ import annotations

import pytest

from kb_factories import ensure_user

from connect.investigation import scoping
from connect.investigation.schema import InvestigationSeed, ScopeDoc
from connect.investigation.scoping import (
    EventLite,
    reaction_candidate_pairs,
    scan_causal_markers,
)
from connect.retrieval.search import rrf_fuse
from connect.storage.pg import utc_now


def ev(event_id, occurred_on, entities, centroid, *, window_days=7,
       title=None):
    return EventLite(
        event_id=event_id, title=title or f"E{event_id}",
        occurred_on=occurred_on, window_days=window_days,
        entity_ids=frozenset(entities),
        centroid=tuple(centroid) if centroid is not None else None)


VEC_A = [1.0, 0.0, 0.0]
VEC_NEAR_A = [0.9, 0.1, 0.0]   # cosine ~0.994
VEC_FAR = [0.0, 1.0, 0.0]      # cosine 0.0


class TestReactionCandidatePairs:
    def test_qualifying_pair_b_after_a(self):
        a = ev(1, "2026-06-01", {1, 2}, VEC_A)
        b = ev(2, "2026-06-06", {2, 3}, VEC_NEAR_A)  # jaccard 1/3 >= 0.3
        out = reaction_candidate_pairs([a, b])
        assert len(out) == 1
        c = out[0]
        assert c.candidate_id == "RC1"
        assert (c.src_event_id, c.dst_event_id) == (2, 1)  # B reacts to A
        assert c.days_apart == 5.0
        assert c.entity_jaccard == pytest.approx(1 / 3, abs=1e-4)
        assert c.cosine > 0.55

    def test_order_matters_no_pair_when_b_before_a(self):
        a = ev(1, "2026-06-10", {1, 2}, VEC_A)
        b = ev(2, "2026-06-05", {1, 2}, VEC_A)
        out = reaction_candidate_pairs([a, b])
        # only the (earlier -> later) direction qualifies
        assert len(out) == 1
        assert (out[0].src_event_id, out[0].dst_event_id) == (1, 2)

    def test_same_day_pairs_are_excluded(self):
        a = ev(1, "2026-06-01", {1, 2}, VEC_A)
        b = ev(2, "2026-06-01", {1, 2}, VEC_A)
        assert reaction_candidate_pairs([a, b]) == []

    def test_window_is_event_type_window_x3(self):
        a = ev(1, "2026-06-01", {1, 2}, VEC_A, window_days=7)
        inside = ev(2, "2026-06-22", {1, 2}, VEC_A)    # day 21 == 7*3
        outside = ev(3, "2026-06-23", {1, 2}, VEC_A)   # day 22 > 21
        out = reaction_candidate_pairs([a, inside])
        assert [(c.src_event_id, c.dst_event_id) for c in out] == [(2, 1)]
        out = reaction_candidate_pairs([a, outside])
        assert out == []

    def test_jaccard_threshold(self):
        a = ev(1, "2026-06-01", {1, 2, 3, 4, 5}, VEC_A)
        b = ev(2, "2026-06-03", {5, 6, 7, 8, 9}, VEC_A)  # jaccard 1/9 < 0.3
        assert reaction_candidate_pairs([a, b]) == []

    def test_cosine_threshold(self):
        a = ev(1, "2026-06-01", {1, 2}, VEC_A)
        b = ev(2, "2026-06-03", {1, 2}, VEC_FAR)  # cosine 0 < 0.55
        assert reaction_candidate_pairs([a, b]) == []

    def test_missing_centroid_or_date_disqualifies(self):
        a = ev(1, "2026-06-01", {1, 2}, VEC_A)
        no_vec = ev(2, "2026-06-03", {1, 2}, None)
        no_date = ev(3, None, {1, 2}, VEC_A)
        assert reaction_candidate_pairs([a, no_vec, no_date]) == []

    def test_existing_causal_edge_blocks_pair_both_directions(self):
        a = ev(1, "2026-06-01", {1, 2}, VEC_A)
        b = ev(2, "2026-06-06", {1, 2}, VEC_A)
        assert reaction_candidate_pairs([a, b],
                                        existing_causal=[(2, 1)]) == []
        assert reaction_candidate_pairs([a, b],
                                        existing_causal=[(1, 2)]) == []

    def test_best_first_ids_and_cap(self):
        a = ev(1, "2026-06-01", {1, 2}, VEC_A)
        strong = ev(2, "2026-06-04", {1, 2}, VEC_A)        # jaccard 1.0
        weak = ev(3, "2026-06-05", {2, 3}, VEC_NEAR_A)     # jaccard 1/3
        out = reaction_candidate_pairs([a, strong, weak])
        assert [c.candidate_id for c in out] == [f"RC{i + 1}"
                                                 for i in range(len(out))]
        assert out[0].src_event_id == 2  # strongest pair first


class TestCausalMarkerScan:
    def test_finds_marker_and_extracts_sentence(self):
        text = ("Some intro sentence. The ministry withdrew the draft "
                "rules in the wake of objections from the committee. "
                "Another sentence.")
        hits = scan_causal_markers([(7, "Doc", text)])
        assert len(hits) == 1
        assert hits[0].document_id == 7
        assert hits[0].marker == "in the wake of"
        assert hits[0].sentence.startswith("The ministry withdrew")
        assert "objections from the committee" in hits[0].sentence

    def test_no_marker_no_hit_and_cap(self):
        assert scan_causal_markers([(1, None, "nothing causal here")]) == []
        docs = [(i, None, "x in response to y.") for i in range(50)]
        assert len(scan_causal_markers(docs, cap=5)) == 5


def test_rrf_fusion_orders_by_reciprocal_rank():
    # doc 3 ranks 1st+2nd; doc 1 ranks 2nd+1st; doc 9 only in one list
    fused = rrf_fuse([[3, 1, 9], [1, 3]])
    assert fused[0] in (1, 3) and fused[1] in (1, 3)
    assert fused[-1] == 9


def test_coverage_thin_heuristics():
    def doc(i, tier=1, url=None):
        return ScopeDoc(document_id=i, credibility_tier=tier,
                        url=url or f"https://site{i}.in/a")
    # fewer than 8 docs -> thin
    assert scoping._coverage([doc(1), doc(2)]) == "thin"
    # 8 docs, 8 domains, tier 1 present -> ok
    assert scoping._coverage([doc(i) for i in range(8)]) == "ok"
    # single domain -> thin
    single = [ScopeDoc(document_id=i, credibility_tier=1,
                       url="https://one.in/x") for i in range(8)]
    assert scoping._coverage(single) == "thin"
    # no tier <= 2 -> thin
    t4 = [doc(i, tier=4) for i in range(8)]
    assert scoping._coverage(t4) == "thin"


class TestResolveSeed:
    async def test_topic_entity_event_story_question(self, db):
        conn = db
        await conn.execute(
            "INSERT INTO entity (name, entity_type, created_at)"
            " VALUES ('RBI', 'organization', %s)", (utc_now(),))
        await conn.execute("INSERT INTO event (title, created_at)"
                           " VALUES ('Repo hike', %s)", (utc_now(),))
        await conn.execute("INSERT INTO story (title, created_at)"
                           " VALUES ('Rate cycle', %s)", (utc_now(),))
        cur = await conn.execute(
            "INSERT INTO dossier (kind, input_text, status, created_at,"
            " owner_id) VALUES ('investigation', 'x', 'pending', %s, %s)"
            " RETURNING id",
            (utc_now(), await ensure_user(conn)))
        dossier_id = (await cur.fetchone())["id"]
        cur = await conn.execute(
            "INSERT INTO question (dossier_id, qtype, text, created_at)"
            " VALUES (%s, 'why_now', 'Why now?', %s) RETURNING id",
            (dossier_id, utc_now()))
        question_id = (await cur.fetchone())["id"]
        assert await scoping.resolve_seed(
            conn, InvestigationSeed(topic=" digital rupee ")) == (
                "digital rupee", "topic", None)
        assert await scoping.resolve_seed(
            conn, InvestigationSeed(entity_id=1)) == ("RBI", "entity",
                                                      None)
        assert await scoping.resolve_seed(
            conn, InvestigationSeed(event_id=1)) == ("Repo hike", "event",
                                                     None)
        assert await scoping.resolve_seed(
            conn, InvestigationSeed(story_id=1)) == ("Rate cycle", "story",
                                                     None)
        # question recursion: topic'd text + parent_question_id
        assert await scoping.resolve_seed(
            conn, InvestigationSeed(question_id=question_id)) == (
                "Why now?", "topic", question_id)

    async def test_missing_rows_raise_lookup_error(self, db):
        with pytest.raises(LookupError):
            await scoping.resolve_seed(db, InvestigationSeed(entity_id=99))
        with pytest.raises(LookupError):
            await scoping.resolve_seed(db,
                                       InvestigationSeed(question_id=99))


def test_seed_validator_exactly_one():
    with pytest.raises(ValueError):
        InvestigationSeed()
    with pytest.raises(ValueError):
        InvestigationSeed(topic="x", entity_id=1)
    with pytest.raises(ValueError):
        InvestigationSeed(topic="   ")
    InvestigationSeed(topic="ok")  # does not raise
