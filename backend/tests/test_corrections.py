"""S3 — corrections, retractions & verdict propagation."""

from __future__ import annotations

from kb_factories import ensure_user, insert_doc, insert_source

from connect.knowledge import corrections
from connect.storage import content_items as item_dao
from connect.storage.pg import utc_now


async def _doc_with_claim(conn, *, verdict: str, text: str = "Body 5%."):
    sid = await insert_source(conn, "RBI", tier=1)
    did = await insert_doc(conn, source_id=sid, text=text)
    cur = await conn.execute(
        "INSERT INTO claim (text, verdict, created_at)"
        " VALUES ('a claim', %s, %s) RETURNING id", (verdict, utc_now()))
    claim_id = (await cur.fetchone())["id"]
    await conn.execute(
        "INSERT INTO claim_sighting (claim_id, document_id, stance, created_at)"
        " VALUES (%s,%s,'asserts',%s)", (claim_id, did, utc_now()))
    return sid, did, claim_id


async def _item(conn, *, did: int, status: str) -> int:
    uid = await ensure_user(conn)
    cur = await conn.execute(
        "INSERT INTO campaign (owner_id, subject, input_type, created_at)"
        " VALUES (%s,'s','topic',%s) RETURNING id", (uid, utc_now()))
    cid = (await cur.fetchone())["id"]
    item_id = await item_dao.insert(
        conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={"headline": "h"},
        sources=[{"ref": "E1", "document_id": did, "quote": "q"}],
        grounding={}, card_shas=[], visibility="shared")
    if status != "draft":
        await conn.execute("UPDATE content_item SET status=%s WHERE id=%s",
                           (status, item_id))
    return item_id


async def test_verdict_flip_retracts_published_and_flags_drafts(pg_conn):
    _, did, claim_id = await _doc_with_claim(pg_conn, verdict="supported")
    pub = await _item(pg_conn, did=did, status="published")
    draft = await _item(pg_conn, did=did, status="draft")

    cid = await corrections.record_verdict_flip(
        pg_conn, claim_id=claim_id, from_verdict="supported",
        to_verdict="refuted")
    assert cid is not None
    counts = await corrections.propagate_open(pg_conn)
    assert counts == {"corrections": 1, "items": 2}

    got = {r.id: r.status for r in [
        await item_dao.get(pg_conn, pub, viewer=1),
        await item_dao.get(pg_conn, draft, viewer=1)]}
    assert got[pub] == "retracted"        # live -> retracted (refuted)
    assert got[draft] == "flagged"        # in-review -> flagged
    # the correction is now acknowledged and links both items
    links = await corrections.list_for_item(pg_conn, pub)
    assert links and links[0]["to_verdict"] == "refuted"


async def test_mixed_flip_marks_published_corrected(pg_conn):
    _, did, claim_id = await _doc_with_claim(pg_conn, verdict="supported")
    pub = await _item(pg_conn, did=did, status="published")
    await corrections.record_verdict_flip(
        pg_conn, claim_id=claim_id, from_verdict="supported",
        to_verdict="mixed")
    await corrections.propagate_open(pg_conn)
    item = await item_dao.get(pg_conn, pub, viewer=1)
    assert item.status == "corrected"     # contested, not outright retracted


async def test_record_verdict_flip_is_idempotent(pg_conn):
    _, did, claim_id = await _doc_with_claim(pg_conn, verdict="supported")
    a = await corrections.record_verdict_flip(
        pg_conn, claim_id=claim_id, from_verdict="supported",
        to_verdict="refuted")
    b = await corrections.record_verdict_flip(
        pg_conn, claim_id=claim_id, from_verdict="supported",
        to_verdict="refuted")
    assert a is not None and b is None    # second open flip is suppressed


async def test_propagate_is_idempotent(pg_conn):
    _, did, claim_id = await _doc_with_claim(pg_conn, verdict="supported")
    pub = await _item(pg_conn, did=did, status="published")
    await corrections.record_verdict_flip(
        pg_conn, claim_id=claim_id, from_verdict="supported",
        to_verdict="refuted")
    first = await corrections.propagate_open(pg_conn)
    second = await corrections.propagate_open(pg_conn)
    assert first["items"] == 1 and second["items"] == 0  # nothing re-acted


async def test_is_propagating_flip():
    assert corrections.is_propagating_flip("supported", "refuted")
    assert corrections.is_propagating_flip("refuted", "supported")
    assert not corrections.is_propagating_flip("supported", "supported")
    assert not corrections.is_propagating_flip("supported", "unverified")
    assert not corrections.is_propagating_flip(None, "refuted")


async def test_update_verdict_opens_a_correction_on_flip(pg_conn):
    from connect.analysis import writeback
    _, did, claim_id = await _doc_with_claim(pg_conn, verdict="supported")
    # a flip supported -> refuted must open a correction...
    await writeback.update_verdict(
        pg_conn, claim_id=claim_id, verdict="refuted", confidence=0.9,
        dossier_id=1, evidence=[])
    cur = await pg_conn.execute(
        "SELECT kind, to_verdict FROM correction WHERE claim_id=%s", (claim_id,))
    row = await cur.fetchone()
    assert row["kind"] == "verdict_flip" and row["to_verdict"] == "refuted"
    # ...but a no-op re-confirm (refuted -> refuted) must not
    await writeback.update_verdict(
        pg_conn, claim_id=claim_id, verdict="refuted", confidence=0.9,
        dossier_id=1, evidence=[])
    cur = await pg_conn.execute(
        "SELECT count(*) AS n FROM correction WHERE claim_id=%s", (claim_id,))
    assert (await cur.fetchone())["n"] == 1


# -- source re-check ----------------------------------------------------------

async def test_recheck_detects_changed_figure(pg_conn):
    sid, did, _ = await _doc_with_claim(
        pg_conn, verdict="supported", text="Inflation was 4.8% in May.")
    out = await corrections.recheck_document(
        pg_conn, did, gone=False, new_text="Inflation was 5.9% in May.")
    assert out == "edited"
    cur = await pg_conn.execute(
        "SELECT kind FROM correction WHERE document_id=%s", (did,))
    assert (await cur.fetchone())["kind"] == "source_edit"
    # source recheck cursor stamped
    cur = await pg_conn.execute(
        "SELECT last_rechecked_at FROM source WHERE id=%s", (sid,))
    assert (await cur.fetchone())["last_rechecked_at"] is not None


async def test_recheck_unchanged_is_noop(pg_conn):
    _, did, _ = await _doc_with_claim(
        pg_conn, verdict="supported", text="Inflation was 4.8% in May.")
    out = await corrections.recheck_document(
        pg_conn, did, gone=False, new_text="Inflation was 4.8% in May.")
    assert out is None
    cur = await pg_conn.execute(
        "SELECT count(*) AS n FROM correction WHERE document_id=%s", (did,))
    assert (await cur.fetchone())["n"] == 0


async def test_recheck_gone_opens_retraction(pg_conn):
    _, did, _ = await _doc_with_claim(pg_conn, verdict="supported")
    out = await corrections.recheck_document(
        pg_conn, did, gone=True, new_text=None)
    assert out == "retracted"
    cur = await pg_conn.execute(
        "SELECT kind FROM correction WHERE document_id=%s", (did,))
    assert (await cur.fetchone())["kind"] == "source_retraction"
