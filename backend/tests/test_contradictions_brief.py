"""Phase 3 consumption surfaces: /api/contradictions (list filters +
dismiss) and the brief's contradiction / trending_claim / suggestion
sections (deterministic scoring + template reasons). No LLM anywhere."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from kb_factories import add_mentions, insert_doc, insert_source

from connect.api.main import create_app
from connect.knowledge import briefing, contradictions
from connect.llm.spend import today_utc
from connect.storage.pg import utc_now


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


async def make_claim(conn, text, verdict="unverified"):
    cur = await conn.execute(
        "INSERT INTO claim (text, verdict, created_at) VALUES (%s,%s,%s)"
        " RETURNING id",
        (text, verdict, utc_now()))
    return int((await cur.fetchone())["id"])


async def add_sighting(conn, claim_id, doc_id, stance="asserts"):
    await conn.execute(
        "INSERT INTO claim_sighting (claim_id, document_id, stance,"
        " created_at) VALUES (%s,%s,%s,%s)",
        (claim_id, doc_id, stance, utc_now()))


async def add_evidence(conn, claim_id, doc_id, stance):
    await conn.execute(
        "INSERT INTO evidence (claim_id, document_id, stance, grade,"
        " created_at) VALUES (%s,%s,%s,2,%s)",
        (claim_id, doc_id, stance, utc_now()))


# --- /api/contradictions ----------------------------------------------------------

async def test_contradictions_endpoints(env, db):
    client, container = env
    conn = db
    pib = await insert_source(conn, "PIB", tier=1)
    et = await insert_source(conn, "ET", tier=2)
    claim_id = await make_claim(conn, "GST revenue doubled",
                                verdict="mixed")
    await add_evidence(conn, claim_id,
                       await insert_doc(conn, source_id=pib), "supports")
    await add_evidence(conn, claim_id,
                       await insert_doc(conn, source_id=et), "refutes")
    assert await contradictions.scan(conn) == 1

    page = client.get("/api/contradictions").json()
    assert page["total"] == 1
    row = page["items"][0]
    assert row["claim"] == {"id": claim_id, "text": "GST revenue doubled",
                            "verdict": "mixed"}
    assert (row["n_support"], row["n_refute"]) == (1, 1)
    assert (row["best_tier_support"], row["best_tier_refute"]) == (1, 2)
    assert row["status"] == "open"
    assert row["detected_at"]

    # status filter
    assert client.get("/api/contradictions?status=open"
                      ).json()["total"] == 1
    assert client.get("/api/contradictions?status=dismissed"
                      ).json()["total"] == 0
    assert client.get("/api/contradictions?status=bogus"
                      ).status_code == 422

    # dismiss -> 200 row; reflected in filters; 404 for unknown ids
    res = client.post(f"/api/contradictions/{row['id']}/dismiss")
    assert res.status_code == 200
    assert res.json()["status"] == "dismissed"
    assert client.get("/api/contradictions?status=open"
                      ).json()["total"] == 0
    assert client.get("/api/contradictions?status=dismissed"
                      ).json()["total"] == 1
    assert client.post("/api/contradictions/999/dismiss"
                       ).status_code == 404


# --- brief: contradiction section ----------------------------------------------------

async def test_brief_contradiction_section(env, db):
    client, container = env
    conn = db
    pib = await insert_source(conn, "PIB", tier=1)
    et = await insert_source(conn, "ET", tier=2)
    claim_id = await make_claim(conn, "Repo rate was hiked twice")
    await add_evidence(conn, claim_id,
                       await insert_doc(conn, source_id=pib), "supports")
    await add_evidence(conn, claim_id,
                       await insert_doc(conn, source_id=et), "refutes")
    await contradictions.scan(conn)

    sections = client.get("/api/brief/today").json()["sections"]
    items = sections["contradiction"]
    assert len(items) == 1
    item = items[0]
    assert item["object_type"] == "contradiction"
    assert item["reason"] == \
        "Disputed: 1 support vs 1 refute · best tiers 1/2"
    assert item["payload"]["title"] == "Repo rate was hiked twice"
    assert item["payload"]["claim_id"] == claim_id


# --- brief: trending_claim section -----------------------------------------------------

async def test_brief_trending_section(env, db):
    client, container = env
    conn = db
    a = await insert_source(conn, "ET", tier=2)
    b = await insert_source(conn, "Scroll", tier=3)
    trending = await make_claim(conn, "Centre to widen PLI scheme")
    await add_sighting(conn, trending, await insert_doc(conn, source_id=a))
    await add_sighting(conn, trending, await insert_doc(conn, source_id=b))
    # one source only -> below the threshold, never trends
    quiet = await make_claim(conn, "Single-source claim")
    await add_sighting(conn, quiet, await insert_doc(conn, source_id=a))

    sections = client.get("/api/brief/today").json()["sections"]
    items = sections["trending_claim"]
    assert [i["object_id"] for i in items] == [trending]
    item = items[0]
    assert item["object_type"] == "claim"
    assert item["reason"] == "2 sources in 48h"
    assert item["payload"]["sources_48h"] == 2
    assert item["payload"]["tiers"] == {"2": 1, "3": 1}


# --- brief: suggestion scoring -----------------------------------------------------------

async def test_suggestion_scoring_components_and_reason(env, db):
    """A claim firing contradiction(2) + trending(1) + watch(2) +
    official-gap(1) scores 6; a trending-only claim scores
    trending(1) + official-gap(1) = 2; ordering follows the score."""
    client, container = env
    conn = db
    et = await insert_source(conn, "ET", tier=2)
    scroll = await insert_source(conn, "Scroll", tier=3)

    hot = await make_claim(conn, "SEBI fined the exchange")
    d1 = await insert_doc(conn, source_id=et)
    d2 = await insert_doc(conn, source_id=scroll)
    await add_sighting(conn, hot, d1)
    await add_sighting(conn, hot, d2)
    await add_evidence(conn, hot, d1, "supports")
    await add_evidence(conn, hot, d2, "refutes")
    await contradictions.scan(conn)
    await add_mentions(conn, d1, ("SEBI",))
    cur = await conn.execute("SELECT id FROM entity WHERE name='SEBI'")
    entity_id = (await cur.fetchone())["id"]
    await conn.execute(
        "INSERT INTO watch (kind, label, entity_id, created_at)"
        " VALUES ('entity', 'SEBI', %s, %s)", (entity_id, utc_now()))

    warm = await make_claim(conn, "Two-source trending claim")
    await add_sighting(conn, warm, await insert_doc(conn, source_id=et))
    await add_sighting(conn, warm,
                       await insert_doc(conn, source_id=scroll))

    sections = client.get("/api/brief/today").json()["sections"]
    items = sections["suggestion"]
    assert [i["object_id"] for i in items] == [hot, warm]

    top = items[0]
    assert top["payload"]["score"] == 6
    assert "Disputed across sources (1 support vs 1 refute)" in top["reason"]
    assert "2 sources in 48h" in top["reason"]
    assert "touches watch: SEBI" in top["reason"]
    assert "no tier-1 source yet" in top["reason"]
    assert top["reason"].count("·") == 3  # template parts joined

    assert items[1]["payload"]["score"] == 2
    assert items[1]["reason"] == "2 sources in 48h · no tier-1 source yet"


async def test_suggestion_calendar_and_tier1_components(container, db):
    """Calendar proximity (+1) fires for a claim whose linked event falls
    <= 90d before a calendar entry; a tier-1 sighting suppresses the
    official-gap component."""
    conn = db
    pib = await insert_source(conn, "PIB", tier=1)
    et = await insert_source(conn, "ET", tier=2)
    claim_id = await make_claim(conn, "Scheme announced before the polls")
    d1 = await insert_doc(conn, source_id=pib)
    d2 = await insert_doc(conn, source_id=et)
    await add_sighting(conn, claim_id, d1)
    await add_sighting(conn, claim_id, d2)

    today = today_utc()
    cur = await conn.execute(
        "INSERT INTO event (title, event_type, occurred_on, created_at)"
        " VALUES ('Scheme launch', 'scheme_announcement', %s, %s)"
        " RETURNING id",
        (today, utc_now()))
    event_id = int((await cur.fetchone())["id"])
    await conn.execute(
        "INSERT INTO event_assignment (document_id, event_id, method,"
        " created_at) VALUES (%s,%s, 'attach', %s)",
        (d1, event_id, utc_now()))
    await conn.execute(
        "INSERT INTO calendar_event (kind, scope, occurs_on, label)"
        " VALUES ('election', 'BR', %s::date + 30, 'Bihar election')",
        (today,))

    drafts = await briefing._suggestion_items(conn, today)
    assert len(drafts) == 1
    draft = drafts[0]
    # trending(1) + calendar(1); tier-1 source present -> NO official gap
    assert draft.components["score"] == 2
    assert "calendar" in draft.components
    assert draft.components["calendar"]["days_before"] == 30
    assert "30d before Bihar election" in draft.reason
    assert "no tier-1 source yet" not in draft.reason
    assert "official_gap" not in draft.components
