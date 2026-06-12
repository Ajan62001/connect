"""Workspace-manager agent — the bounded governed tool-loop chat endpoint and
the focus/tenancy scoping of its tools. MockProvider scripted turns, no
network."""

from __future__ import annotations

from kb_factories import ensure_user, insert_doc, insert_source
from mock_llm import MockProvider, tool_turn

from connect.agents.workspace_agent import (
    WorkspaceAgentState,
    WorkspaceToolExecutor,
)
from connect.llm.provider import LLMError, Usage
from connect.storage import workspaces as workspace_dao
from connect.storage.pg import utc_now
from dbutil import q1, qv


def _set_llm(client, provider) -> None:
    client.app.state.container.llm = provider


def _chat(client, wid: int, message: str, chat_id: int | None = None):
    payload: dict = {"message": message}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    return client.post(f"/api/workspaces/{wid}/chat", json=payload)


# ---------------------------------------------------------------------------
# the chat loop (endpoint, scripted MockProvider)
# ---------------------------------------------------------------------------

async def test_chat_grounded_qa(client, db):
    src = await insert_source(db, "RBI", tier=1)
    await insert_doc(db, title="RBI holds repo rate", source_id=src,
                     text="The RBI kept the repo rate unchanged at 6.5%.")
    wid = client.post("/api/workspaces", json={
        "name": "Rates", "query_fts": "repo rate"}).json()["id"]

    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("search_workspace", {"query": "repo rate"})),
        tool_turn(("final_answer", {"answer": "The RBI held the repo rate at 6.5%."})),
    ]))
    r = _chat(client, wid, "what happened to the repo rate?")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "The RBI held the repo rate at 6.5%."
    assert body["tools_used"] == ["search_workspace", "final_answer"]
    assert body["turns_completed"] == 2
    assert body["finding"] is None
    # both turns metered under the workspace_agent purpose, attributed to me
    me = client.get("/api/me").json()
    assert await qv(db, "SELECT COUNT(*) FROM llm_call"
                        " WHERE purpose = 'workspace_agent'") == 2
    assert await qv(db, "SELECT COUNT(*) FROM llm_call WHERE"
                        " purpose = 'workspace_agent' AND user_id = %s",
                    me["id"]) == 2


async def test_chat_posts_finding_to_workspace(client, db):
    src = await insert_source(db, "RBI", tier=1)
    doc_id = await insert_doc(db, title="RBI holds repo rate", source_id=src,
                              text="The RBI kept the repo rate at 6.5%.")
    wid = client.post("/api/workspaces", json={
        "name": "Rates", "query_fts": "repo rate"}).json()["id"]

    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("search_workspace", {"query": "repo rate"})),
        tool_turn(("post_finding", {"title": "Pause likely",
                                    "body": "Signals a hold.",
                                    "document_id": doc_id})),
        tool_turn(("final_answer", {"answer": "Posted a finding."})),
    ]))
    r = _chat(client, wid, "capture a finding")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["finding"] is not None
    assert body["finding"]["workspace_id"] == wid
    assert body["finding"]["document_id"] == doc_id
    # surfaced by the workspace findings filter
    scoped = client.get("/api/posts", params={"workspace_id": wid}).json()
    assert [p["id"] for p in scoped] == [body["finding"]["id"]]


# ---------------------------------------------------------------------------
# tool scoping (executor unit tests)
# ---------------------------------------------------------------------------

async def _executor(container, db, *, viewer: int, **ws_kwargs):
    ws = await workspace_dao.insert(db, owner_id=viewer, name="WS", **ws_kwargs)
    state = WorkspaceAgentState()
    ex = WorkspaceToolExecutor(
        db, embedder=container.embedder, vectors=container.vectors,
        workspace=ws, viewer=viewer, state=state)
    return ex


async def test_search_scoped_to_workspace_focus(container, db):
    me = await ensure_user(db, email="wsa@test.local")
    src_a = await insert_source(db, "RBI", tier=1)
    src_b = await insert_source(db, "Other", tier=3)
    in_focus = await insert_doc(db, title="RBI repo rate hold", source_id=src_a,
                                text="the repo rate was held by the RBI")
    off_focus = await insert_doc(db, title="market repo chatter", source_id=src_b,
                                 text="market talk about the repo rate today")

    ex = await _executor(container, db, viewer=me, source_ids=[src_a])
    out = await ex._tool_search_workspace({"query": "repo rate"})
    ids = {r["document_id"] for r in out["results"]}
    assert in_focus in ids
    assert off_focus not in ids   # different source -> outside the lens


async def test_search_respects_tenancy(container, db):
    me = await ensure_user(db, email="wsb@test.local")
    other = await ensure_user(db, email="wsb-other@test.local")
    src = await insert_source(db, "RBI", tier=1)
    mine = await insert_doc(db, title="shared repo doc", source_id=src,
                            text="the repo rate held steady")
    # a private doc owned by ANOTHER user, matching the query
    cur = await db.execute(
        "INSERT INTO document (source_id, title, fetched_at, media_type,"
        " content_text, content_hash, enrichment_status, visibility, owner_id)"
        " VALUES (%s,'secret repo doc',%s,'text','the repo rate secretly fell',"
        " 'h-wsa-priv','pending','private',%s) RETURNING id",
        (src, utc_now(), other))
    private = (await cur.fetchone())["id"]

    ex = await _executor(container, db, viewer=me, query_fts="repo rate")
    out = await ex._tool_search_workspace({"query": "repo rate"})
    ids = {r["document_id"] for r in out["results"]}
    assert mine in ids
    assert private not in ids     # another user's private doc never surfaces

    # read_document on it is refused too
    refused = await ex._tool_read_document({"document_id": private})
    assert refused.is_error


# ---------------------------------------------------------------------------
# governance: budget + iteration caps, error mapping
# ---------------------------------------------------------------------------

async def test_budget_forces_final_answer(client, db):
    await insert_doc(db, title="doc", text="the repo rate held")
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    # huge usage blows the per-turn USD cap after turn 1 -> turn 2 is forced
    big = Usage(input_tokens=50_000_000, output_tokens=50_000_000)
    provider = MockProvider(usage=big, tool_turns=[
        tool_turn(("search_workspace", {"query": "x"}), usage=big),
        tool_turn(("final_answer", {"answer": "forced stop"}), usage=big),
    ])
    _set_llm(client, provider)
    r = _chat(client, wid, "hi")
    assert r.status_code == 200, r.text
    # the second turn was forced to final_answer
    assert provider.calls[1]["tool_choice"] == "final_answer"
    assert r.json()["reply"] == "forced stop"


async def test_daily_budget_429_before_first_turn(client, db):
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    me = client.get("/api/me").json()
    await db.execute(
        "INSERT INTO llm_call (purpose, model, input_tokens, output_tokens,"
        " cache_read_tokens, cost_estimate, created_at, user_id)"
        " VALUES ('analysis','claude-sonnet-4-6',1,1,0,50.0,%s,%s)",
        (utc_now(), me["id"]))
    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("final_answer", {"answer": "x"}))]))
    r = _chat(client, wid, "hi")
    assert r.status_code == 429
    # nothing was spent on the agent
    assert await qv(db, "SELECT COUNT(*) FROM llm_call"
                        " WHERE purpose = 'workspace_agent'") == 0


async def test_iteration_cap(client, db):
    await insert_doc(db, title="doc", text="repo rate")
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    # the agent never calls final_answer -> the loop force-stops at the cap
    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("search_workspace", {"query": "x"})) for _ in range(10)]))
    r = _chat(client, wid, "hi")
    assert r.status_code == 200, r.text
    assert r.json()["turns_completed"] == 6   # WS_AGENT_MAX_ITERS
    assert await qv(db, "SELECT COUNT(*) FROM llm_call"
                        " WHERE purpose = 'workspace_agent'") == 6


def test_chat_keyless_503(client):
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    _set_llm(client, None)
    r = _chat(client, wid, "hi")
    assert r.status_code == 503


def test_chat_llm_error_502(client):
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]

    def boom(_record):
        raise LLMError("provider exploded")

    _set_llm(client, MockProvider(tool_turns=boom))
    r = _chat(client, wid, "hi")
    assert r.status_code == 502


def test_chat_validation_422(client):
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("final_answer", {"answer": "x"}))]))
    # whitespace-only message passes min_length but the endpoint strips -> 422
    assert _chat(client, wid, "   ").status_code == 422
    # missing message field -> pydantic 422
    assert client.post(f"/api/workspaces/{wid}/chat", json={}).status_code == 422


async def test_chat_persists_and_resumes(client, db):
    await insert_doc(db, title="doc", text="the repo rate held")
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    _set_llm(client, MockProvider(tool_turns=lambda _r: tool_turn(
        ("final_answer", {"answer": "ok"}))))

    # first message starts a new chat
    first = _chat(client, wid, "first question").json()
    chat_id = first["chat_id"]
    assert chat_id and len(first["transcript"]) == 2   # user + assistant
    assert first["transcript"][0]["text"] == "first question"

    # it shows up in the chat list (title from the first message)
    chats = client.get(f"/api/workspaces/{wid}/chats").json()
    assert [c["id"] for c in chats] == [chat_id]
    assert chats[0]["title"] == "first question"

    # a follow-up continues the SAME chat
    second = _chat(client, wid, "follow up", chat_id=chat_id).json()
    assert second["chat_id"] == chat_id
    assert len(second["transcript"]) == 4              # two exchanges

    # GET the saved chat returns the full transcript
    detail = client.get(f"/api/workspaces/{wid}/chats/{chat_id}").json()
    assert [t["text"] for t in detail["transcript"]] == [
        "first question", "ok", "follow up", "ok"]
    # still exactly one chat
    assert len(client.get(f"/api/workspaces/{wid}/chats").json()) == 1

    # delete it
    assert client.delete(
        f"/api/workspaces/{wid}/chats/{chat_id}").status_code == 204
    assert client.get(f"/api/workspaces/{wid}/chats").json() == []
    assert client.get(
        f"/api/workspaces/{wid}/chats/{chat_id}").status_code == 404


async def test_chat_of_another_user_is_404(client, db):
    """A chat is private to its owner — another workspace member can't read it."""
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    me = client.get("/api/me").json()
    other = await ensure_user(db, email="chat-other@test.local")
    # a chat owned by another user
    from connect.storage import workspace_chats as chat_dao
    cid = await chat_dao.create(db, workspace_id=wid, owner_id=other,
                                title="theirs", messages=[], transcript=[])
    assert client.get(f"/api/workspaces/{wid}/chats/{cid}").status_code == 404
    assert client.delete(
        f"/api/workspaces/{wid}/chats/{cid}").status_code == 404
    # and it does not appear in MY list
    assert me["id"] != other
    assert client.get(f"/api/workspaces/{wid}/chats").json() == []
