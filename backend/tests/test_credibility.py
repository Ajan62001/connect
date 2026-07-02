"""S4 — dynamic, auditable source-credibility scoring."""

from __future__ import annotations

from kb_factories import insert_doc, insert_source

from connect.analysis.stages import verify
from connect.knowledge import credibility
from connect.storage.pg import utc_now


# -- the pure math ------------------------------------------------------------

def test_reliability_no_record_returns_prior():
    assert credibility.reliability(aligned_w=0, total_w=0, prior=0.8) == 0.8
    assert credibility.reliability(aligned_w=0, total_w=0, prior=0.5) == 0.5


def test_reliability_strong_alignment_rises_above_prior():
    # a tier-3 source (prior 0.5) that is consistently right climbs
    s = credibility.reliability(aligned_w=40, total_w=40, prior=0.5)
    assert 0.5 < s <= 1.0


def test_reliability_floored_for_consistently_wrong_source():
    # a tier-1 source (prior 1.0) that is consistently WRONG cannot fall below
    # FLOOR_FRAC × prior, however much misaligned evidence accrues
    s = credibility.reliability(aligned_w=0, total_w=100, prior=1.0)
    assert s == credibility.FLOOR_FRAC * 1.0


def test_source_weight_backward_compatible():
    assert verify.source_weight(1, None) == verify.tier_weight(1)
    assert verify.source_weight(3, None) == verify.tier_weight(3)
    assert verify.source_weight(1, 0.37) == 0.37


def test_weigh_uses_reliability_when_present():
    docs = [
        verify.WeighedDoc(document_id=1, domain="a.test", credibility_tier=4,
                          stance="supports", relevance=1.0, reliability=0.95),
        verify.WeighedDoc(document_id=2, domain="b.test", credibility_tier=4,
                          stance="supports", relevance=1.0, reliability=None),
    ]
    w = verify.weigh(docs)
    assert w[0] == 0.95                       # reliability overrides tier-4 0.3
    assert w[1] == verify.tier_weight(4)      # None falls back to the tier


# -- the recompute over a real track record -----------------------------------

async def _evidence(conn, *, source_id, stance, verdict):
    did = await insert_doc(conn, source_id=source_id, text="body")
    cur = await conn.execute(
        "INSERT INTO claim (text, verdict, verdict_updated_at, created_at)"
        " VALUES ('c', %s, %s, %s) RETURNING id", (verdict, utc_now(), utc_now()))
    claim_id = (await cur.fetchone())["id"]
    await conn.execute(
        "INSERT INTO evidence (claim_id, document_id, stance, method, grade,"
        " created_at) VALUES (%s,%s,%s,'llm',2,%s)",
        (claim_id, did, stance, utc_now()))


async def test_recompute_rewards_aligned_source(pg_conn):
    sid = await insert_source(pg_conn, "Reliable", tier=3)   # prior 0.5
    for _ in range(12):
        await _evidence(pg_conn, source_id=sid, stance="supports",
                        verdict="supported")
    score = await credibility.recompute_one(pg_conn, sid, trigger="test")
    assert score > 0.5                                       # climbed above prior
    src = await __import__(
        "connect.storage.sources", fromlist=["get"]).get(pg_conn, sid)
    assert abs(src.reliability_score - score) < 1e-9
    # an audit row was written
    cur = await pg_conn.execute(
        "SELECT sample_size, trigger FROM source_credibility_history"
        " WHERE source_id=%s", (sid,))
    row = await cur.fetchone()
    assert row["sample_size"] == 12 and row["trigger"] == "test"


async def test_recompute_penalises_misaligned_source(pg_conn):
    sid = await insert_source(pg_conn, "Unreliable", tier=2)  # prior 0.8
    for _ in range(12):
        await _evidence(pg_conn, source_id=sid, stance="supports",
                        verdict="refuted")                    # always wrong
    score = await credibility.recompute_one(pg_conn, sid, trigger="test")
    assert score < 0.8 and score >= credibility.FLOOR_FRAC * 0.8


async def test_recompute_all_enabled(pg_conn):
    a = await insert_source(pg_conn, "A", tier=1)
    b = await insert_source(pg_conn, "B", tier=4)
    scores = await credibility.recompute(pg_conn)
    assert set(scores) == {a, b}
    # no track record -> exactly the tier prior
    assert scores[a] == verify.tier_weight(1)
    assert scores[b] == verify.tier_weight(4)
