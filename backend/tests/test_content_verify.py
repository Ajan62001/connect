"""S2 — the content_verify re-verification path (ContentService.verify_item).

Exercises the async gate run that reconstructs evidence from the S1 provenance
link rows, persists a refreshed GateReport, and promotes an item out of the
'verifying' holding state.
"""

from __future__ import annotations

from kb_factories import ensure_user, insert_doc, insert_source
from mock_llm import MockProvider

from connect.analysis.entailment import EntailmentJudgment
from connect.storage import content_items as item_dao
from connect.storage import jobs as job_dao
from connect.storage.pg import utc_now


def _provider(label: str) -> MockProvider:
    return MockProvider(respond_by_schema={
        EntailmentJudgment: lambda _u: EntailmentJudgment(label=label)})


async def _seed_verifying_item(conn, *, headline: str, quote: str) -> tuple[int, int]:
    uid = await ensure_user(conn)
    sid = await insert_source(conn, "RBI", tier=1)
    did = await insert_doc(conn, source_id=sid, text=quote)
    cur = await conn.execute(
        "INSERT INTO campaign (owner_id, subject, input_type, created_at)"
        " VALUES (%s,'inflation','topic',%s) RETURNING id", (uid, utc_now()))
    cid = (await cur.fetchone())["id"]
    item_id = await item_dao.insert(
        conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={"headline": headline, "caption": "see more"},
        sources=[{"ref": "E1", "document_id": did, "quote": quote}],
        grounding={}, card_shas=[], visibility="shared")
    await item_dao.set_status(conn, item_id, owner_id=uid, status="verifying",
                              from_statuses=("draft",))
    return item_id, uid


async def test_verify_item_records_gate_and_promotes(container):
    container.content.provider = _provider("not_entailed")
    async with container.pool.connection() as conn:
        item_id, uid = await _seed_verifying_item(
            conn, headline="Inflation soared to 19%",
            quote="Retail inflation eased to 4.8% in May.")
    async with container.pool.connection() as conn:
        verdict = await container.content.verify_item(conn, item_id)
    assert verdict == "flagged"
    async with container.pool.connection() as conn:
        item = await item_dao.get(conn, item_id, viewer=uid)
    # warn-only (default): promoted to approved even though the gate flagged it,
    # and the flag is recorded for the reviewer to see.
    assert item.status == "approved"
    assert item.gate.get("verdict") == "flagged"
    assert item.gate.get("flagged")


async def test_verify_item_clean_item_passes(container):
    container.content.provider = _provider("entailed")
    async with container.pool.connection() as conn:
        item_id, uid = await _seed_verifying_item(
            conn, headline="Inflation eased to 4.8%",
            quote="Retail inflation eased to 4.8% in May.")
    async with container.pool.connection() as conn:
        verdict = await container.content.verify_item(conn, item_id)
    assert verdict == "pass"
    async with container.pool.connection() as conn:
        item = await item_dao.get(conn, item_id, viewer=uid)
    assert item.status == "approved"
    assert item.gate.get("verdict") == "pass"


async def test_verify_promotes_after_midflight_approve(container, monkeypatch):
    """Regression: promotion out of 'verifying' is a CAS on the LIVE status,
    not verify_item's row snapshot — an approve that flips the item to
    'verifying' while the gate's LLM calls run must still be promoted out."""
    import connect.content.service as service_mod

    container.content.provider = _provider("entailed")
    async with container.pool.connection() as conn:
        item_id, uid = await _seed_verifying_item(
            conn, headline="Inflation eased to 4.8%",
            quote="Retail inflation eased to 4.8% in May.")
        # back to draft: the approve happens MID-verify (below)
        await conn.execute(
            "UPDATE content_item SET status = 'draft' WHERE id = %s",
            (item_id,))

    real_assess = service_mod.gate_mod.assess

    async def assess_then_approve(*a, **kw):
        # simulate an enforcing-mode approve landing while the gate runs
        async with container.pool.connection() as c2:
            await item_dao.set_status(c2, item_id, owner_id=uid,
                                      status="verifying",
                                      from_statuses=("draft",))
        return await real_assess(*a, **kw)

    monkeypatch.setattr(service_mod.gate_mod, "assess", assess_then_approve)

    async with container.pool.connection() as conn:
        verdict = await container.content.verify_item(conn, item_id)
    assert verdict == "pass"
    async with container.pool.connection() as conn:
        item = await item_dao.get(conn, item_id, viewer=uid)
    assert item.status == "approved"    # promoted despite the stale snapshot


async def test_verify_and_correction_jobs_dedup(db):
    """content_verify dedups per item WHILE QUEUED and content_correction is a
    live singleton (the partial unique indexes + _DEDUP_KEYS/_SINGLETON_KINDS
    in jobs.py); a deduped enqueue returns the EXISTING job instead of
    raising."""
    a = await job_dao.create(db, "content_verify", {"item_id": 7})
    b = await job_dao.create(db, "content_verify",
                             {"item_id": 7, "promote_to": "approved"})
    assert b == a
    assert await job_dao.create(db, "content_verify", {"item_id": 8}) != a

    # a RUNNING verify must NOT swallow a fresh enqueue: it snapshotted its
    # item row at claim time, so edits/approvals since need their own job
    await db.execute("UPDATE job SET status = 'running' WHERE id = %s", (a,))
    c = await job_dao.create(db, "content_verify", {"item_id": 7})
    assert c != a

    s1 = await job_dao.create(db, "content_correction", {})
    s2 = await job_dao.create(db, "content_correction", {})
    assert s2 == s1
    # ...even while the sweep runs (it fans out itself; two must never race)
    await db.execute("UPDATE job SET status = 'running' WHERE id = %s", (s1,))
    assert await job_dao.create(db, "content_correction", {}) == s1

    # a terminal job frees the key for a fresh enqueue
    await db.execute("UPDATE job SET status = 'done' WHERE id = %s", (c,))
    assert await job_dao.create(db, "content_verify", {"item_id": 7}) != c
