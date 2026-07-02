"""S6 — the reader-facing trust report (knowledge/trust.py)."""

from __future__ import annotations

from kb_factories import ensure_user, insert_doc, insert_source

from connect.knowledge import trust
from connect.storage import content_items as item_dao
from connect.storage.pg import utc_now


async def _item_with_two_sources(conn):
    uid = await ensure_user(conn)
    s1 = await insert_source(conn, "RBI", tier=1)
    s2 = await insert_source(conn, "Mint", tier=3)
    await conn.execute(
        "UPDATE source SET reliability_score = 0.92 WHERE id = %s", (s1,))
    d1 = await insert_doc(conn, source_id=s1, text="a")
    d2 = await insert_doc(conn, source_id=s2, text="b")
    cur = await conn.execute(
        "INSERT INTO campaign (owner_id, subject, input_type, created_at)"
        " VALUES (%s,'s','topic',%s) RETURNING id", (uid, utc_now()))
    cid = (await cur.fetchone())["id"]
    item_id = await item_dao.insert(
        conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={"headline": "h"},
        sources=[{"ref": "E1", "document_id": d1, "source_name": "RBI",
                  "quote": "q1"},
                 {"ref": "E2", "document_id": d2, "source_name": "Mint",
                  "quote": "q2"}],
        grounding={}, card_shas=[], visibility="shared",
        gate={"verdict": "pass", "checked": 4, "flagged": [], "contested": []})
    return item_id, uid


async def test_trust_report_assembles_source_mix(pg_conn):
    item_id, _ = await _item_with_two_sources(pg_conn)
    item = await item_dao.get(pg_conn, item_id, viewer=1)
    rep = await trust.build_trust_report(pg_conn, item_id, gate=item.gate)

    assert len(rep.sources) == 2
    assert rep.tier_mix == {"1": 1, "3": 1}             # tier snapshot from S1
    assert rep.independent_publishers == 2
    assert rep.balance_label in ("moderate", "broad")
    assert rep.one_sided is False
    assert rep.ai_disclosure is True
    # reliability (S4) rode through for the RBI source
    rbi = next(s for s in rep.sources if s.source_name == "RBI")
    assert rbi.reliability_score == 0.92
    assert rbi.credibility_tier == 1
    # clean gate -> healthy confidence
    assert rep.gate_verdict == "pass" and rep.confidence >= 0.8


async def test_trust_report_reflects_flags_and_contested(pg_conn):
    item_id, _ = await _item_with_two_sources(pg_conn)
    gate = {"verdict": "flagged", "checked": 4,
            "flagged": [{"reason": "not_entailed"}],
            "contested": [{"claim_id": 1, "verdict": "refuted"}]}
    rep = await trust.build_trust_report(pg_conn, item_id, gate=gate)
    assert rep.gate_verdict == "flagged" and rep.flagged_count == 1
    assert rep.confidence < 0.6                          # flag pulls it down
    assert rep.contested and rep.contested[0]["verdict"] == "refuted"


async def test_trust_report_single_source_is_one_sided(pg_conn):
    uid = await ensure_user(pg_conn)
    s1 = await insert_source(pg_conn, "OnlyOne", tier=2)
    d1 = await insert_doc(pg_conn, source_id=s1, text="a")
    cur = await pg_conn.execute(
        "INSERT INTO campaign (owner_id, subject, input_type, created_at)"
        " VALUES (%s,'s','topic',%s) RETURNING id", (uid, utc_now()))
    cid = (await cur.fetchone())["id"]
    item_id = await item_dao.insert(
        pg_conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={"headline": "h"},
        sources=[{"ref": "E1", "document_id": d1, "source_name": "OnlyOne"}],
        grounding={}, card_shas=[], visibility="shared")
    rep = await trust.build_trust_report(pg_conn, item_id, gate={})
    assert rep.independent_publishers == 1
    assert rep.one_sided is True and rep.balance_label == "single"
