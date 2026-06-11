"""Story threading: follow-up candidate rules, follows-edge creation, and
union-find story maintenance (create / adopt / merge)."""

from __future__ import annotations

from kb_factories import t1_doc
from mock_llm import MockProvider

from connect.knowledge.enrichment import t2
from connect.knowledge.linking import event_clusterer as ec
from connect.knowledge.linking import story_threader as st
from connect.llm.spend import Governor
from connect.storage.db import utc_now

V_A = [1.0, 0.0]
V_B = [0.0, 1.0]

BILL_ENTITIES = ("Data Protection Bill", "Ministry of Electronics and IT")


def governor(conn, budget=2.0):
    return Governor(conn, budget)


async def make_event(conn, *, event_type="bill_stage",
                      entities=BILL_ENTITIES, vec=V_A, title="Event",
                      provider=None):
    doc = t1_doc(conn, title=title, event_type=event_type,
                 entities=entities, vec=vec)
    result = await ec.assign_document(conn, provider, governor(conn), doc)
    assert result.created_event, "fixture must create a fresh event"
    return result.event_id, doc


def follow_provider(answer: bool) -> MockProvider:
    return MockProvider(respond_by_schema={
        st.FollowUpJudgment: st.FollowUpJudgment(is_follow_up=answer)})


# --- candidate rules ---------------------------------------------------------------

async def test_candidates_require_two_shared_entities(container):
    conn = container.db
    a, _ = await make_event(conn, title="Bill introduced")
    # one shared entity only -> not a candidate
    b, _ = await make_event(
        conn, title="Unrelated bill",
        entities=("Data Protection Bill", "Some Other Body"), vec=V_B)
    assert st.find_follow_candidates(conn, b) == []


async def test_candidates_require_related_lifecycle(container):
    conn = container.db
    a, _ = await make_event(conn, title="Bill introduced")  # legislative
    # same entities but unrelated event_type (different group, not same type)
    b, _ = await make_event(conn, title="Court case", vec=V_B,
                            event_type="court_ruling")  # judicial
    assert st.find_follow_candidates(conn, b) == []
    # ordinance shares the 'legislative' lifecycle_group with bill_stage
    c, _ = await make_event(conn, title="Ordinance route", vec=[0.7, 0.7],
                            event_type="ordinance")
    cand_ids = [r["id"] for r in st.find_follow_candidates(conn, c)]
    assert a in cand_ids


async def test_same_ungrouped_type_is_related(container):
    conn = container.db
    a, _ = await make_event(conn, title="Statement 1",
                            event_type="statement")  # lifecycle_group NULL
    b, _ = await make_event(conn, title="Statement 2", vec=V_B,
                            event_type="statement")
    cand_ids = [r["id"] for r in st.find_follow_candidates(conn, b)]
    assert a in cand_ids


# --- the follows edge + story creation through the full T2 path ----------------------

async def test_follow_up_creates_edge_and_story(container):
    conn = container.db
    a, _ = await make_event(conn, title="Bill introduced in Lok Sabha")
    provider = follow_provider(True)
    doc2 = t1_doc(conn, title="Bill passed by Lok Sabha",
                  event_type="bill_stage", entities=BILL_ENTITIES, vec=V_B)
    stats = await t2.process_document(conn, provider, governor(conn), doc2,
                                      triggers=["manual"])
    assert stats["created_event"]
    b = stats["event_id"]
    assert stats["threading"]["followed_event_id"] == a
    story_id = stats["threading"]["story_id"]
    assert story_id is not None

    # the follows edge: B -follows-> A with the triggering doc as provenance
    edge = conn.execute(
        "SELECT * FROM edge WHERE relation='follows'").fetchone()
    assert (edge["src_type"], edge["src_id"]) == ("event", b)
    assert (edge["dst_type"], edge["dst_id"]) == ("event", a)
    assert edge["provenance_document_id"] == doc2
    assert edge["status"] == "active"

    # both events joined the story; title/root/doc_count materialized
    rows = conn.execute("SELECT id, story_id FROM event ORDER BY id"
                        ).fetchall()
    assert all(r["story_id"] == story_id for r in rows)
    story = conn.execute("SELECT * FROM story WHERE id=?",
                         (story_id,)).fetchone()
    assert story["root_event_id"] == a
    assert story["title"] == "Bill introduced in Lok Sabha"
    assert story["doc_count"] == 2
    # follow-up judgment cost was ledgered
    assert conn.execute("SELECT COUNT(*) FROM llm_call WHERE purpose="
                        "'story_followup'").fetchone()[0] == 1
    # document promoted to tier 2
    assert conn.execute("SELECT enrichment_tier FROM document WHERE id=?",
                        (doc2,)).fetchone()[0] == 2


async def test_follow_up_no_means_no_edge(container):
    conn = container.db
    await make_event(conn, title="Bill introduced")
    provider = follow_provider(False)
    b, _ = await make_event(conn, title="Different bill", vec=V_B,
                            provider=provider)
    threading = await st.thread_new_event(conn, provider, governor(conn),
                                          b, provenance_document_id=None)
    assert threading["followed_event_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE relation='follows'"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM story").fetchone()[0] == 0


async def test_no_provider_skips_judgment(container):
    conn = container.db
    await make_event(conn, title="Bill introduced")
    b, _ = await make_event(conn, title="Bill passed", vec=V_B)
    threading = await st.thread_new_event(conn, None, governor(conn), b,
                                          provenance_document_id=None)
    assert threading["candidates"] == 1
    assert threading["followed_event_id"] is None


# --- union-find story maintenance ------------------------------------------------------

def insert_event(conn, title, occurred_on, doc_count=1):
    with conn:
        cur = conn.execute(
            "INSERT INTO event (title, event_type, occurred_on, doc_count,"
            " created_at) VALUES (?, 'other', ?, ?, ?)",
            (title, occurred_on, doc_count, utc_now()))
    return int(cur.lastrowid)


def test_merge_creates_story_when_neither_has_one(container):
    conn = container.db
    a = insert_event(conn, "A", "2026-06-01", doc_count=2)
    b = insert_event(conn, "B", "2026-06-05", doc_count=3)
    sid = st.merge_into_story(conn, b, a)
    story = conn.execute("SELECT * FROM story WHERE id=?", (sid,)).fetchone()
    assert story["root_event_id"] == a  # earliest event is the root
    assert story["title"] == "A"
    assert story["doc_count"] == 5
    assert {r[0] for r in conn.execute(
        "SELECT story_id FROM event")} == {sid}


def test_merge_adopts_existing_story(container):
    conn = container.db
    a = insert_event(conn, "A", "2026-06-01")
    b = insert_event(conn, "B", "2026-06-02")
    sid = st.merge_into_story(conn, b, a)
    c = insert_event(conn, "C", "2026-06-03", doc_count=4)
    assert st.merge_into_story(conn, c, b) == sid
    assert conn.execute("SELECT story_id FROM event WHERE id=?",
                        (c,)).fetchone()[0] == sid
    assert conn.execute("SELECT doc_count FROM story WHERE id=?",
                        (sid,)).fetchone()[0] == 6


def test_merge_unions_two_stories(container):
    conn = container.db
    a = insert_event(conn, "A", "2026-06-01")
    b = insert_event(conn, "B", "2026-06-02")
    s1 = st.merge_into_story(conn, b, a)
    c = insert_event(conn, "C", "2026-06-03")
    d = insert_event(conn, "D", "2026-06-04")
    s2 = st.merge_into_story(conn, d, c)
    assert s1 != s2

    survivor = st.merge_into_story(conn, d, b)  # bridges the two stories
    assert survivor == min(s1, s2)
    assert conn.execute("SELECT COUNT(*) FROM story").fetchone()[0] == 1
    assert {r[0] for r in conn.execute("SELECT story_id FROM event")} \
        == {survivor}
    story = conn.execute("SELECT * FROM story WHERE id=?",
                         (survivor,)).fetchone()
    assert story["root_event_id"] == a
    assert story["title"] == "A"
    assert story["doc_count"] == 4
