"""S1 — content-to-evidence provenance graph (content_item_source).

Covers the dual-write from content_items.insert (JSONB + relational rows in one
transaction), the credibility-tier snapshot resolved from the cited document's
source, the reverse index list_for_document, ON DELETE CASCADE, and the
v18->v19 backfill that seeds link rows from pre-existing JSONB sources.
"""

from __future__ import annotations

from connect.storage import content_items, content_sources, migrations
from connect.storage import pg as pg_mod


async def _user(conn, email="ed@example.com") -> int:
    cur = await conn.execute(
        "INSERT INTO app_user (email, created_at) VALUES (%s, %s) RETURNING id",
        (email, pg_mod.utc_now()))
    return int((await cur.fetchone())["id"])


async def _source(conn, name="RBI", tier=1) -> int:
    cur = await conn.execute(
        "INSERT INTO source (name, type, credibility_tier, created_at)"
        " VALUES (%s, 'rss', %s, %s) RETURNING id",
        (name, tier, pg_mod.utc_now()))
    return int((await cur.fetchone())["id"])


async def _document(conn, source_id, content_hash="h1") -> int:
    cur = await conn.execute(
        "INSERT INTO document (source_id, content_hash, content_text,"
        " fetched_at) VALUES (%s, %s, 'body', %s) RETURNING id",
        (source_id, content_hash, pg_mod.utc_now()))
    return int((await cur.fetchone())["id"])


async def _campaign(conn, owner_id) -> int:
    cur = await conn.execute(
        "INSERT INTO campaign (owner_id, subject, input_type, created_at)"
        " VALUES (%s, 'GST', 'topic', %s) RETURNING id",
        (owner_id, pg_mod.utc_now()))
    return int((await cur.fetchone())["id"])


async def test_insert_dual_writes_links_and_snapshots_tier(pg_conn):
    uid = await _user(pg_conn)
    sid = await _source(pg_conn, tier=2)
    did = await _document(pg_conn, sid)
    cid = await _campaign(pg_conn, uid)

    sources = [
        {"ref": "E1", "document_id": did, "source_name": "RBI",
         "quote": "rates held", "quote_start": 10, "quote_end": 20},
        {"ref": "E2", "url": "https://x.test/a", "source_name": "web"},  # no doc
    ]
    item_id = await content_items.insert(
        pg_conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={"headline": "hi"}, sources=sources,
        grounding={"menu_size": 2}, card_shas=[], visibility="shared")

    # JSONB sources preserved (dual-write, not replaced)
    cur = await pg_conn.execute(
        "SELECT jsonb_array_length(sources) AS n FROM content_item WHERE id=%s",
        (item_id,))
    assert (await cur.fetchone())["n"] == 2

    rows = await content_sources.list_for_item(pg_conn, item_id)
    assert [r["ref"] for r in rows] == ["E1", "E2"]
    e1 = next(r for r in rows if r["ref"] == "E1")
    assert e1["document_id"] == did
    assert e1["credibility_tier"] == 2          # snapshotted from the source
    assert (e1["quote_start"], e1["quote_end"]) == (10, 20)
    e2 = next(r for r in rows if r["ref"] == "E2")
    assert e2["document_id"] is None
    assert e2["credibility_tier"] is None        # no document -> no tier


async def test_list_for_document_reverse_index(pg_conn):
    uid = await _user(pg_conn)
    sid = await _source(pg_conn)
    did = await _document(pg_conn, sid)
    cid = await _campaign(pg_conn, uid)
    src = [{"ref": "E1", "document_id": did, "quote": "q"}]
    a = await content_items.insert(
        pg_conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={}, sources=src, grounding={}, card_shas=[],
        visibility="shared")
    b = await content_items.insert(
        pg_conn, campaign_id=cid, owner_id=uid, platform="x",
        format="x_thread", content={}, sources=src, grounding={}, card_shas=[],
        visibility="shared")
    items = {r["content_item_id"] for r in
             await content_sources.list_for_document(pg_conn, did)}
    assert items == {a, b}


async def test_cascade_delete_removes_links(pg_conn):
    uid = await _user(pg_conn)
    sid = await _source(pg_conn)
    did = await _document(pg_conn, sid)
    cid = await _campaign(pg_conn, uid)
    item_id = await content_items.insert(
        pg_conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={}, sources=[{"ref": "E1", "document_id": did}],
        grounding={}, card_shas=[], visibility="shared")
    await pg_conn.execute("DELETE FROM content_item WHERE id=%s", (item_id,))
    assert await content_sources.list_for_item(pg_conn, item_id) == []
    # document delete only NULLs the link's document_id (provenance survives)
    item2 = await content_items.insert(
        pg_conn, campaign_id=cid, owner_id=uid, platform="instagram",
        format="ig_card", content={}, sources=[{"ref": "E1", "document_id": did}],
        grounding={}, card_shas=[], visibility="shared")
    await pg_conn.execute("DELETE FROM document WHERE id=%s", (did,))
    rows = await content_sources.list_for_item(pg_conn, item2)
    assert len(rows) == 1 and rows[0]["document_id"] is None


async def test_migrate_18_to_19_backfills_from_jsonb(pg_conn):
    """Drop to a v18 shape (no link table), seed a content_item carrying JSONB
    sources, then run the migration and assert it backfills link rows with the
    tier snapshot — idempotent on a second run."""
    uid = await _user(pg_conn)
    sid = await _source(pg_conn, tier=3)
    did = await _document(pg_conn, sid)
    cid = await _campaign(pg_conn, uid)
    # simulate v18: no provenance rows for this item
    await pg_conn.execute("DROP TABLE content_item_source")
    import json
    payload = json.dumps([
        {"ref": "E1", "document_id": did, "quote": "q", "source_name": "RBI"},
        {"ref": "E2", "url": "https://x.test"},
    ])
    cur = await pg_conn.execute(
        "INSERT INTO content_item (campaign_id, owner_id, platform, format,"
        " status, content, sources, created_at) VALUES"
        " (%s,%s,'instagram','ig_card','draft','{}'::jsonb,%s::jsonb,%s)"
        " RETURNING id", (cid, uid, payload, pg_mod.utc_now()))
    item_id = int((await cur.fetchone())["id"])

    await migrations.pg_migrate_18_to_19(pg_conn)
    rows = await content_sources.list_for_item(pg_conn, item_id)
    assert [r["ref"] for r in rows] == ["E1", "E2"]
    assert next(r for r in rows if r["ref"] == "E1")["credibility_tier"] == 3

    # idempotent: re-running does not duplicate
    await migrations.pg_migrate_18_to_19(pg_conn)
    assert len(await content_sources.list_for_item(pg_conn, item_id)) == 2
