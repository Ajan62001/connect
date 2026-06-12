"""Phase C tenancy — THE LEAK MATRIX (user B must never see user A's
private document through any read surface), the three I1 write gates
(enrichment eligibility, analysis writeback, investigation record_finding
+ deferred edges), the share cascade (confirmation list, deferred-edge
materialization, the 409 paths), per-user briefs/watches isolation, and
dossier visibility defaults. NO network; MockProvider only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import login
from dbutil import q1, qv
from kb_factories import ensure_user, insert_dossier, insert_doc, \
    insert_source
from mock_llm import MockProvider

from connect.analysis.budget import AnalysisBudget
from connect.analysis.context import AnalysisContext
from connect.analysis.schema import (
    ClaimReasoning,
    DecomposedClaim,
    NormalizedInput,
    StanceJudgment,
)
from connect.analysis.stages import verify
from connect.api.main import create_app
from connect.auth import sessions as auth_sessions
from connect.auth.sessions import SESSION_COOKIE
from connect.investigation.schema import (
    InvestigationSeed,
    ReactionCandidate,
    ScopePack,
)
from connect.investigation.writeback import (
    FindingValidationError,
    record_finding,
)
from connect.knowledge.embedder import NullEmbedder
from connect.llm.spend import Governor
from connect.retrieval.search_client import NullSearchClient
from connect.storage import users as user_dao
from connect.storage.pg import utc_now

TOKEN = "ZEPHYRQUARTZ"  # the unique marker that must never leak
PRIVATE_TEXT = (f"The confidential memo {TOKEN} alleges the draft rules"
                " were withdrawn after committee objections.")


# --- the two-user environment -------------------------------------------------

@pytest.fixture()
def env(settings):
    """One app; A = the dev-login admin, B = an invited member with a real
    session row. Identity switches via the connect_session cookie."""
    app = create_app(settings)
    with TestClient(app) as client:
        me = login(client)
        sid_a = client.cookies.get(SESSION_COOKIE)
        yield {"client": client, "container": app.state.container,
               "a_id": me["id"], "sid_a": sid_a}


async def make_user_b(env, db, settings, email="bee@test.local"):
    row = await user_dao.insert(db, email=email, role="member")
    sid = await auth_sessions.create_session(db, row["id"], settings)
    return int(row["id"]), sid


def act_as(env, sid: str) -> TestClient:
    """Switch the client's identity: clear the jar first — httpx keeps
    server-set and test-set cookies as distinct entries otherwise, and the
    request would carry BOTH sessions."""
    env["client"].cookies.clear()
    env["client"].cookies.set(SESSION_COOKIE, sid)
    return env["client"]


def ingest_private_text(client, text=PRIVATE_TEXT, title=None) -> dict:
    resp = client.post("/api/documents",
                       json={"text": text, **({"title": title} if title
                                              else {})})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["visibility"] == "private"  # §1 default for pasted text
    assert body["origin"] == "user_text"
    return body


# ==============================================================================
# THE LEAK MATRIX — table-driven over every read surface
# ==============================================================================

LEAK_SURFACES = [
    ("documents_list", "/api/documents?page_size=100"),
    ("documents_fts", f"/api/documents?q={TOKEN}"),
    ("search", f"/api/search?q={TOKEN}"),
    ("feed", "/api/feed?page_size=100"),
]


@pytest.mark.parametrize("name,url", LEAK_SURFACES)
async def test_leak_matrix_read_surfaces(env, db, settings, name, url):
    client = env["client"]
    doc = ingest_private_text(client)               # A's private doc
    _, sid_b = await make_user_b(env, db, settings)

    # the owner sees their own private doc on every surface
    resp_a = act_as(env, env["sid_a"]).get(url)
    assert resp_a.status_code == 200
    assert TOKEN in resp_a.text, f"A cannot see own doc via {name}"
    ids_a = {i["id"] for i in (resp_a.json().get("items")
                               or resp_a.json().get("documents"))}
    assert doc["id"] in ids_a

    # B sees NOTHING
    resp_b = act_as(env, sid_b).get(url)
    assert resp_b.status_code == 200
    assert TOKEN not in resp_b.text, f"LEAK via {name}"


async def test_leak_matrix_document_detail_and_patch(env, db, settings):
    client = env["client"]
    doc = ingest_private_text(client)
    _, sid_b = await make_user_b(env, db, settings)

    assert act_as(env, env["sid_a"]).get(
        f"/api/documents/{doc['id']}").status_code == 200
    b = act_as(env, sid_b)
    assert b.get(f"/api/documents/{doc['id']}").status_code == 404
    # B cannot flip its visibility either (undisclosed -> 404, not 403)
    assert b.patch(f"/api/documents/{doc['id']}",
                   json={"visibility": "shared"}).status_code == 404


async def test_leak_matrix_linked_from_and_link_fetch(env, db, settings):
    """A shared doc must not reveal its PRIVATE parents through
    linked_from, and B cannot drive link-follow on a private doc's link."""
    client = act_as(env, env["sid_a"])
    private = ingest_private_text(client)
    shared = await insert_doc(db, title="Public doc", text="public body")
    cur = await db.execute(
        "INSERT INTO document_link (document_id, url, status,"
        " resolved_document_id, created_at)"
        " VALUES (%s, 'https://x.example/a', 'fetched', %s, %s)"
        " RETURNING id",
        (private["id"], shared, utc_now()))
    link_id = (await cur.fetchone())["id"]

    detail_a = client.get(f"/api/documents/{shared}").json()
    assert [p["document_id"] for p in detail_a["linked_from"]] \
        == [private["id"]]

    _, sid_b = await make_user_b(env, db, settings)
    b = act_as(env, sid_b)
    detail_b = b.get(f"/api/documents/{shared}").json()
    assert detail_b["linked_from"] == []            # private parent hidden
    assert TOKEN not in str(detail_b)
    # the link row hangs off A's private doc: B gets 404
    assert b.post(f"/api/document-links/{link_id}/fetch").status_code == 404


async def test_leak_matrix_brief_items_per_user(env, db, settings):
    """Watch-driven brief isolation: A's private doc fires only A's watch;
    B's brief stays empty; B cannot mark A's brief item seen."""
    b_id, sid_b = await make_user_b(env, db, settings)
    a = act_as(env, env["sid_a"])
    assert a.post("/api/watches", json={
        "kind": "topic", "label": "A token", "query_fts": TOKEN,
    }).status_code == 201
    b = act_as(env, sid_b)
    assert b.post("/api/watches", json={
        "kind": "topic", "label": "B token", "query_fts": TOKEN,
    }).status_code == 201

    a = act_as(env, env["sid_a"])
    ingest_private_text(a)                          # fires A's watch only

    brief_a = a.get("/api/brief/today").json()
    items_a = brief_a["sections"]["watch_dev"]
    assert len(items_a) == 1 and TOKEN in str(items_a)

    b = act_as(env, sid_b)
    brief_b = b.get("/api/brief/today").json()
    assert brief_b["sections"]["watch_dev"] == []
    assert TOKEN not in str(brief_b)
    assert brief_b["brief"]["id"] != brief_a["brief"]["id"]  # per-user rows
    # B cannot mark A's item seen
    assert b.post(
        f"/api/brief/items/{items_a[0]['id']}/seen").status_code == 404
    # ... while A can
    assert act_as(env, env["sid_a"]).post(
        f"/api/brief/items/{items_a[0]['id']}/seen").status_code == 204


async def test_leak_matrix_derived_tables_stay_empty(env, db, settings):
    """The I1 payoff: a private doc never reaches the derived tables, so
    entity pages / statements / sightings have nothing to leak."""
    client = act_as(env, env["sid_a"])
    ingest_private_text(client)
    for table in ("document_enrichment", "entity_mention", "statement",
                  "claim_sighting", "entity"):
        assert await qv(db, f"SELECT count(*) FROM {table}") == 0, table
    _, sid_b = await make_user_b(env, db, settings)
    resp = act_as(env, sid_b).get(f"/api/entities?q={TOKEN}")
    assert resp.json()["items"] == []


async def test_leak_matrix_private_dossiers(env, db, settings):
    """B must not see A's private analysis/investigation via list, detail,
    SSE, or cancel; A's shared ones carry owner attribution."""
    a_id = env["a_id"]
    private_a = await insert_dossier(db, kind="analysis",
                                     input_text=f"claim {TOKEN}",
                                     visibility="private", owner_id=a_id)
    shared_a = await insert_dossier(db, kind="analysis",
                                    input_text="public claim",
                                    visibility="shared", owner_id=a_id)
    private_inv = await insert_dossier(db, kind="investigation",
                                       input_text=f"why {TOKEN}",
                                       visibility="private", owner_id=a_id)

    _, sid_b = await make_user_b(env, db, settings)
    b = act_as(env, sid_b)

    page = b.get("/api/analyses").json()
    assert {i["id"] for i in page["items"]} == {shared_a}
    assert TOKEN not in str(page)
    shared_item = page["items"][0]
    assert shared_item["owner_id"] == a_id          # attribution
    assert shared_item["visibility"] == "shared"

    assert b.get(f"/api/analyses/{private_a}").status_code == 404
    assert b.get(f"/api/analyses/{private_a}/events").status_code == 404
    assert b.post(f"/api/analyses/{private_a}/cancel").status_code == 404
    assert b.get(f"/api/analyses/{shared_a}").status_code == 200
    # B may see the shared one but not cancel it (member, not owner)
    assert b.post(f"/api/analyses/{shared_a}/cancel").status_code == 403

    inv_page = b.get("/api/investigations").json()
    assert all(i["id"] != private_inv for i in inv_page["items"])
    assert b.get(f"/api/investigations/{private_inv}").status_code == 404
    assert b.get(
        f"/api/investigations/{private_inv}/events").status_code == 404

    # the owner sees everything
    a = act_as(env, env["sid_a"])
    assert {i["id"] for i in a.get("/api/analyses").json()["items"]} \
        == {private_a, shared_a}
    assert a.get(f"/api/analyses/{private_a}").status_code == 200


async def test_watch_and_cursor_isolation(env, db, settings):
    a = act_as(env, env["sid_a"])
    watch = a.post("/api/watches", json={
        "kind": "topic", "label": "mine", "query_fts": "x"}).json()
    _, sid_b = await make_user_b(env, db, settings)
    b = act_as(env, sid_b)
    assert b.get("/api/watches").json() == []
    assert b.patch(f"/api/watches/{watch['id']}",
                   json={"label": "stolen"}).status_code == 404
    assert b.delete(f"/api/watches/{watch['id']}").status_code == 404
    assert b.post(f"/api/watches/{watch['id']}/seen").status_code == 404
    assert b.get("/api/watches/badges").json() == {}
    # cursors land on separate per-user rows
    assert b.post("/api/cursors",
                  json={"surface": "feed", "ref_id": 0}).status_code == 204
    a = act_as(env, env["sid_a"])
    assert a.post("/api/cursors",
                  json={"surface": "feed", "ref_id": 0}).status_code == 204
    assert await qv(db, "SELECT count(*) FROM view_cursor") == 2


# ==============================================================================
# Gate 1 — enrichment eligibility
# ==============================================================================

async def test_gate1_private_doc_never_enriched_until_shared(env, db):
    client = act_as(env, env["sid_a"])
    doc = ingest_private_text(client)
    service = env["container"].enrichment

    eligible = await service.select_eligible(db)
    assert doc["id"] not in [r["id"] for r in eligible]
    # the fast path refuses too — same predicate
    service.provider = MockProvider()
    status = await service.enrich_document(db, doc["id"])
    assert status.startswith("skipped: private")
    assert await qv(db, "SELECT enrichment_status FROM document"
                        " WHERE id = %s", doc["id"]) == "pending"
    # manual T2 promote: blocked at the route
    assert client.post(
        f"/api/documents/{doc['id']}/promote").status_code == 409

    # sharing requires NO special action — the next sweep picks it up
    assert client.patch(f"/api/documents/{doc['id']}",
                        json={"visibility": "shared"}).status_code == 200
    eligible = await service.select_eligible(db)
    assert doc["id"] in [r["id"] for r in eligible]


async def test_document_unshare_blocked_after_derivatives(env, db):
    """shared->private on a document 409s once it compounded into the
    shared KB (I1 would break retroactively)."""
    client = act_as(env, env["sid_a"])
    doc = ingest_private_text(client)
    client.patch(f"/api/documents/{doc['id']}",
                 json={"visibility": "shared"})
    # un-share is fine while nothing derived exists...
    assert client.patch(f"/api/documents/{doc['id']}",
                        json={"visibility": "private"}).status_code == 200
    client.patch(f"/api/documents/{doc['id']}",
                 json={"visibility": "shared"})
    # ... but not after enrichment-grade rows appear
    await db.execute(
        "INSERT INTO document_enrichment (document_id, summary, event_type,"
        " model, prompt_version, created_at)"
        " VALUES (%s, 's', 'other', 'm', 't1-v2', %s)",
        (doc["id"], utc_now()))
    resp = client.patch(f"/api/documents/{doc['id']}",
                        json={"visibility": "private"})
    assert resp.status_code == 409


async def test_document_patch_requires_owner(env, db, settings):
    """A shared doc is visible to B but only the OWNER flips visibility."""
    client = act_as(env, env["sid_a"])
    doc = ingest_private_text(client)
    client.patch(f"/api/documents/{doc['id']}",
                 json={"visibility": "shared"})
    _, sid_b = await make_user_b(env, db, settings)
    resp = act_as(env, sid_b).patch(f"/api/documents/{doc['id']}",
                                    json={"visibility": "private"})
    assert resp.status_code == 403


# ==============================================================================
# Gate 2 — analysis writeback
# ==============================================================================

COMMON_SENTENCE = "The repo rate was raised by 25 basis points."


def _stance_provider():
    return MockProvider(
        respond=StanceJudgment(stance="supports",
                               quoted_span=COMMON_SENTENCE,
                               relevance=1.0, note=""),
        respond_by_schema={ClaimReasoning: ClaimReasoning(
            reasoning="ok", cited_evidence_ids=[])})


def _ctx(pool, conn, *, dossier_id, shared, viewer):
    return AnalysisContext(
        conn=conn, provider=_stance_provider(),
        governor=Governor(pool, 100.0), budget=AnalysisBudget(5.0),
        search=NullSearchClient(), ingest=None, embedder=NullEmbedder(),
        vectors=None, dossier_id=dossier_id, max_evidence_per_claim=6,
        emit=_noop_emit, shared=shared, viewer=viewer)


async def _noop_emit(t, d):
    return 0


def _normalized():
    return NormalizedInput(
        subject="rbi", input_kind="news_claim",
        claims=[DecomposedClaim(id="C1",
                                text="repo rate raised basis points",
                                kind="factual", checkable=True)])


async def test_gate2_private_analysis_writes_nothing_shared(env, db):
    a_id = env["a_id"]
    src = await insert_source(db, "ET", tier=2)
    await insert_doc(db, title="RBI hike", text=COMMON_SENTENCE,
                     source_id=src)
    dossier = await insert_dossier(db, kind="analysis",
                                   input_text="claim",
                                   visibility="private", owner_id=a_id)
    ctx = _ctx(env["container"].pool, db, dossier_id=dossier,
               shared=False, viewer=a_id)
    results, _ = await verify.run(ctx, _normalized())
    # full verification ran — but ONLY into the dossier-scoped result
    assert results[0].verdict == "supported"
    assert results[0].claim_id < 0                  # synthetic, not canonical
    assert all(e.evidence_id < 0 for e in results[0].evidence)
    for table in ("claim", "evidence", "verdict_history", "claim_embedding"):
        assert await qv(db, f"SELECT count(*) FROM {table}") == 0, table


async def test_gate2_shared_analysis_drops_private_doc_evidence(env, db):
    """A SHARED dossier owned by A gathers A's private doc as evidence
    (viewer = owner) but the evidence ROW lands only for shared docs; the
    verdict snapshot carries no private refs."""
    a_id = env["a_id"]
    src = await insert_source(db, "ET", tier=2)
    shared_doc = await insert_doc(db, title="RBI hike public",
                                  text=COMMON_SENTENCE, source_id=src)
    cur = await db.execute(
        "INSERT INTO document (title, fetched_at, media_type, content_text,"
        " content_hash, enrichment_status, owner_id, visibility, origin)"
        " VALUES ('private repo note', %s, 'text', %s, 'h-priv-g2',"
        " 'pending', %s, 'private', 'user_text') RETURNING id",
        (utc_now(), "Secret note. " + COMMON_SENTENCE, a_id))
    private_doc = (await cur.fetchone())["id"]
    dossier = await insert_dossier(db, kind="analysis", input_text="claim",
                                   visibility="shared", owner_id=a_id)
    ctx = _ctx(env["container"].pool, db, dossier_id=dossier,
               shared=True, viewer=a_id)
    results, _ = await verify.run(ctx, _normalized())

    gathered = {e.document_id for e in results[0].evidence}
    assert {shared_doc, private_doc} <= gathered    # viewer saw both
    evidence_docs = [r["document_id"] for r in (await (await db.execute(
        "SELECT document_id FROM evidence")).fetchall())]
    assert shared_doc in evidence_docs
    assert private_doc not in evidence_docs         # dropped from shared KB
    snapshot = await qv(db, "SELECT evidence_snapshot FROM verdict_history")
    assert all(ref["document_id"] != private_doc for ref in snapshot)


async def test_gate2_viewer_isolation_in_evidence_gathering(env, db,
                                                            settings):
    """B's shared dossier must NOT gather A's private doc as evidence."""
    a_id = env["a_id"]
    b_id, _ = await make_user_b(env, db, settings)
    await db.execute(
        "INSERT INTO document (title, fetched_at, media_type, content_text,"
        " content_hash, enrichment_status, owner_id, visibility, origin)"
        " VALUES ('private', %s, 'text', %s, 'h-priv-vi', 'pending', %s,"
        " 'private', 'user_text')",
        (utc_now(), COMMON_SENTENCE, a_id))
    dossier = await insert_dossier(db, kind="analysis", input_text="claim",
                                   visibility="shared", owner_id=b_id)
    ctx = _ctx(env["container"].pool, db, dossier_id=dossier,
               shared=True, viewer=b_id)
    results, _ = await verify.run(ctx, _normalized())
    assert results[0].evidence == []                # nothing visible to B
    assert await qv(db, "SELECT count(*) FROM evidence") == 0


# ==============================================================================
# Gate 3 — record_finding private-evidence rule + deferred edges
# ==============================================================================

DOC_TEXT = ("The ministry withdrew the draft rules in the wake of "
            "objections from the standing committee.")
QUOTE = DOC_TEXT


async def _gate3_env(db, *, dossier_visibility, dossier_owner,
                     doc_owner, doc_visibility="private"):
    cur = await db.execute(
        "INSERT INTO document (title, fetched_at, media_type, content_text,"
        " content_hash, enrichment_status, owner_id, visibility, origin)"
        " VALUES ('memo', %s, 'text', %s, %s, 'pending', %s, %s,"
        " 'user_text') RETURNING id",
        (utc_now(), DOC_TEXT, f"h-g3-{dossier_owner}-{doc_owner}",
         doc_owner, doc_visibility))
    doc_id = (await cur.fetchone())["id"]
    dossier_id = await insert_dossier(
        db, kind="investigation", input_text="why withdrawn",
        visibility=dossier_visibility, owner_id=dossier_owner)
    await db.execute("INSERT INTO event (title, occurred_on, created_at)"
                     " VALUES ('Objections', '2026-05-20', %s)",
                     (utc_now(),))
    await db.execute("INSERT INTO event (title, occurred_on, created_at)"
                     " VALUES ('Withdrawn', '2026-05-28', %s)",
                     (utc_now(),))
    ev1 = await qv(db, "SELECT min(id) FROM event")
    ev2 = await qv(db, "SELECT max(id) FROM event")
    pack = ScopePack(input_text="draft rules", input_type="topic",
                     reaction_candidates=[])
    args = {"kind": "context", "text": "withdrawn after objections",
            "evidence": [{"document_id": doc_id, "quote": QUOTE}],
            "link": {"src_type": "event", "src_id": ev2,
                     "relation": "reaction_to",
                     "dst_type": "event", "dst_id": ev1}}
    return doc_id, dossier_id, pack, args


async def test_gate3_private_evidence_rejected_in_shared_dossier(env, db):
    a_id = env["a_id"]
    _, dossier_id, pack, args = await _gate3_env(
        db, dossier_visibility="shared", dossier_owner=a_id, doc_owner=a_id)
    with pytest.raises(FindingValidationError, match="is private"):
        await record_finding(db, dossier_id=dossier_id, scope_pack=pack,
                             args=args)
    assert await qv(db, "SELECT count(*) FROM finding") == 0


async def test_gate3_foreign_private_evidence_rejected(env, db, settings):
    a_id = env["a_id"]
    b_id, _ = await make_user_b(env, db, settings)
    _, dossier_id, pack, args = await _gate3_env(
        db, dossier_visibility="private", dossier_owner=b_id,
        doc_owner=a_id)
    with pytest.raises(FindingValidationError, match="is private"):
        await record_finding(db, dossier_id=dossier_id, scope_pack=pack,
                             args=args)


async def test_gate3_private_dossier_defers_edge_then_share_materializes(
        env, db):
    """Own-private evidence inside a private dossier is allowed; the edge
    is DEFERRED into finding.payload['link']; the share cascade asks for
    confirmation, shares the cited doc, and materializes the edge."""
    a_id = env["a_id"]
    doc_id, dossier_id, pack, args = await _gate3_env(
        db, dossier_visibility="private", dossier_owner=a_id,
        doc_owner=a_id)
    result = await record_finding(db, dossier_id=dossier_id,
                                  scope_pack=pack, args=args)
    assert result["edge_id"] is None
    assert result["edge_deferred"] is True
    assert await qv(db, "SELECT count(*) FROM edge") == 0   # deferred
    payload = await qv(db, "SELECT payload FROM finding WHERE id = %s",
                       result["finding_id"])
    assert payload["link"]["relation"] == "reaction_to"

    client = act_as(env, env["sid_a"])
    # shared->private of a SHARED dossier is 409 (checked later); first the
    # cascade: unconfirmed share answers 409 with the confirmation list
    resp = client.patch(f"/api/investigations/{dossier_id}",
                        json={"visibility": "shared"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "confirm_documents"
    assert [d["id"] for d in detail["documents"]] == [doc_id]

    # confirmed: doc auto-shared, deferred edge materialized, dossier shared
    resp = client.patch(f"/api/investigations/{dossier_id}",
                        json={"visibility": "shared",
                              "confirm_documents": True})
    assert resp.status_code == 200
    assert resp.json()["shared_document_ids"] == [doc_id]
    assert await qv(db, "SELECT visibility FROM document WHERE id = %s",
                    doc_id) == "shared"
    edge = await q1(db, "SELECT * FROM edge")
    assert edge is not None and edge["relation"] == "reaction_to"
    assert edge["grade"] == 2
    assert edge["provenance_dossier_id"] == dossier_id
    assert await qv(db, "SELECT edge_id FROM finding WHERE id = %s",
                    result["finding_id"]) == edge["id"]
    assert await qv(db, "SELECT visibility FROM dossier WHERE id = %s",
                    dossier_id) == "shared"

    # ... and now the 409 back-path: shared -> private is forbidden
    resp = client.patch(f"/api/investigations/{dossier_id}",
                        json={"visibility": "private"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "cannot_unshare"


async def test_cascade_no_private_docs_shares_directly(env, db):
    """A private dossier citing only SHARED docs shares without any
    confirmation round-trip."""
    a_id = env["a_id"]
    doc_id, dossier_id, pack, args = await _gate3_env(
        db, dossier_visibility="private", dossier_owner=a_id,
        doc_owner=None, doc_visibility="shared")
    await record_finding(db, dossier_id=dossier_id, scope_pack=pack,
                         args=args)
    resp = act_as(env, env["sid_a"]).patch(
        f"/api/investigations/{dossier_id}",
        json={"visibility": "shared"})
    assert resp.status_code == 200
    assert resp.json()["shared_document_ids"] == []
    assert await qv(db, "SELECT count(*) FROM edge") == 1  # materialized


async def test_cascade_defensive_foreign_private_doc_409(env, db, settings):
    """Impossible by gate 3, checked anyway: a cited private doc owned by
    someone else blocks the share."""
    a_id = env["a_id"]
    b_id, _ = await make_user_b(env, db, settings)
    dossier_id = await insert_dossier(db, kind="investigation",
                                      input_text="x",
                                      visibility="private", owner_id=a_id)
    cur = await db.execute(
        "INSERT INTO document (title, fetched_at, media_type, content_text,"
        " content_hash, enrichment_status, owner_id, visibility, origin)"
        " VALUES ('b-priv', %s, 'text', 'b body', 'h-bpriv', 'pending',"
        " %s, 'private', 'user_text') RETURNING id", (utc_now(), b_id))
    foreign_doc = (await cur.fetchone())["id"]
    cur = await db.execute(
        "INSERT INTO finding (dossier_id, kind, text, created_at)"
        " VALUES (%s, 'context', 'f', %s) RETURNING id",
        (dossier_id, utc_now()))
    finding_id = (await cur.fetchone())["id"]
    await db.execute(
        "INSERT INTO finding_evidence (finding_id, document_id, quote)"
        " VALUES (%s, %s, 'q')", (finding_id, foreign_doc))
    resp = act_as(env, env["sid_a"]).patch(
        f"/api/investigations/{dossier_id}",
        json={"visibility": "shared", "confirm_documents": True})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "foreign_private_documents"
    assert await qv(db, "SELECT visibility FROM dossier WHERE id = %s",
                    dossier_id) == "private"


async def test_patch_visibility_authz(env, db, settings):
    """B: private dossier -> 404 (undisclosed); shared dossier -> 403
    (owner-or-admin only); same-state PATCH is a no-op 200."""
    a_id = env["a_id"]
    private_d = await insert_dossier(db, kind="analysis", input_text="x",
                                     visibility="private", owner_id=a_id)
    shared_d = await insert_dossier(db, kind="analysis", input_text="y",
                                    visibility="shared", owner_id=a_id)
    _, sid_b = await make_user_b(env, db, settings)
    b = act_as(env, sid_b)
    assert b.patch(f"/api/analyses/{private_d}",
                   json={"visibility": "shared"}).status_code == 404
    assert b.patch(f"/api/analyses/{shared_d}",
                   json={"visibility": "shared"}).status_code == 403
    a = act_as(env, env["sid_a"])
    resp = a.patch(f"/api/analyses/{shared_d}",
                   json={"visibility": "shared"})
    assert resp.status_code == 200                  # no-op
    resp = a.patch(f"/api/analyses/{private_d}",
                   json={"visibility": "shared"})
    assert resp.status_code == 200                  # nothing cited -> direct


# ==============================================================================
# Ownership writes / visibility defaults
# ==============================================================================

async def test_analysis_create_sets_owner_and_visibility(env, db):
    container = env["container"]
    assert container.analysis is not None
    container.analysis.provider = MockProvider()    # pass the 503 gate
    a = act_as(env, env["sid_a"])
    accepted = a.post("/api/analyses",
                      json={"input_text": "claim text"}).json()
    row = await q1(db, "SELECT owner_id, visibility FROM dossier"
                       " WHERE id = %s", accepted["analysis_id"])
    assert row["owner_id"] == env["a_id"]
    assert row["visibility"] == "shared"            # §1 default

    accepted = a.post("/api/analyses",
                      json={"input_text": "secret claim",
                            "visibility": "private"}).json()
    row = await q1(db, "SELECT owner_id, visibility FROM dossier"
                       " WHERE id = %s", accepted["analysis_id"])
    assert row["visibility"] == "private"           # explicit opt-in


async def test_child_investigation_inherits_private_from_parent(env, db):
    """A question of a PRIVATE dossier spawns a PRIVATE child (its scope
    descends from private work) — design §1 default rule."""
    a_id = env["a_id"]
    parent = await insert_dossier(db, kind="investigation",
                                  input_text="parent",
                                  visibility="private", owner_id=a_id)
    cur = await db.execute(
        "INSERT INTO question (dossier_id, qtype, text, status, created_at)"
        " VALUES (%s, 'why_now', 'why now?', 'open', %s) RETURNING id",
        (parent, utc_now()))
    question_id = (await cur.fetchone())["id"]
    service = env["container"].investigations
    # no LLM in tests: the enqueued run must fail fast as data (the row
    # checks below are about start(), not the run)
    service.provider = None
    child_id, _ = await service.start(
        InvestigationSeed(question_id=question_id), owner_id=a_id)
    row = await q1(db, "SELECT owner_id, visibility FROM dossier"
                       " WHERE id = %s", child_id)
    assert row["owner_id"] == a_id
    assert row["visibility"] == "private"

    # a topic-seeded investigation defaults shared
    topic_id, _ = await service.start(
        InvestigationSeed(topic="plain topic"), owner_id=a_id)
    assert await qv(db, "SELECT visibility FROM dossier WHERE id = %s",
                    topic_id) == "shared"

    # drain the embedded queue's job tasks (provider is None — the jobs
    # fail as data) before the app context tears the loop down
    import asyncio
    await asyncio.gather(*list(env["container"].jobs._tasks),
                         return_exceptions=True)


async def test_url_ingest_is_shared_system_owned(env, db, settings,
                                                 tmp_path):
    """user-URL and investigation-fetch ingests are public web content:
    shared, owner NULL (design §1) — exercised through the pipeline seam
    (no network: ingest_text stands in for the fetch result)."""
    container = env["container"]
    result = await container.pipeline.ingest_text(
        db, "public page body", title="public page",
        origin="investigation_fetch")
    row = await q1(db, "SELECT owner_id, visibility, origin FROM document"
                       " WHERE id = %s", result.document.id)
    assert row["owner_id"] is None
    assert row["visibility"] == "shared"
    assert row["origin"] == "investigation_fetch"
