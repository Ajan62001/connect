"""Async ("deep") workspace-agent runs — the SAME agent as the chat, run as a
background job that streams job_event progress and lands its answer in the chat
transcript (no separate task row). Runs on the embedded job queue; MockProvider
scripted turns, no network."""

from __future__ import annotations

import asyncio
import time

from kb_factories import insert_doc
from mock_llm import MockProvider, tool_turn

from dbutil import q1


def _set_llm(client, provider) -> None:
    client.app.state.container.llm = provider


async def _wait_for_job(db, job_id: int, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = await q1(db, "SELECT status, error FROM job WHERE id = %s",
                       job_id)
        if row and row["status"] in ("done", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"job {job_id} did not finish: {dict(row) if row else None}")


async def test_async_run_completes_and_lands_in_chat(client, db):
    await insert_doc(db, title="RBI repo", text="The RBI held the repo rate.")
    wid = client.post("/api/workspaces",
                      json={"name": "W", "query_fts": "repo"}).json()["id"]
    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("search_workspace", {"query": "repo rate"})),
        tool_turn(("final_answer", {"answer": "Rates were held."})),
    ]))

    r = client.post(f"/api/workspaces/{wid}/chat/async",
                    json={"message": "summarize the repo-rate news"})
    assert r.status_code == 202, r.text
    chat_id, job_id = r.json()["chat_id"], r.json()["job_id"]

    job = await _wait_for_job(db, job_id)
    assert job["status"] == "done"

    # the answer landed in the chat transcript
    detail = client.get(f"/api/workspaces/{wid}/chats/{chat_id}").json()
    roles = [t["role"] for t in detail["transcript"]]
    assert roles == ["user", "assistant"]
    assert detail["transcript"][-1]["text"] == "Rates were held."

    # progress streamed as job_event 'iteration' rows
    iters = await q1(db, "SELECT count(*) AS n FROM job_event"
                         " WHERE job_id = %s AND type = 'iteration'", job_id)
    assert iters["n"] >= 1

    # the SSE replay closes on the terminal 'done' event
    sse = client.get(f"/api/workspaces/{wid}/chats/{chat_id}/events").text
    assert "event: iteration" in sse
    assert "event: done" in sse


async def test_async_run_continues_existing_chat(client, db):
    await insert_doc(db, title="RBI repo", text="The RBI held the repo rate.")
    wid = client.post("/api/workspaces",
                      json={"name": "W", "query_fts": "repo"}).json()["id"]
    # a synchronous quick turn first, to create the chat
    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("final_answer", {"answer": "Hi."}))]))
    chat_id = client.post(f"/api/workspaces/{wid}/chat",
                          json={"message": "hello"}).json()["chat_id"]

    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("final_answer", {"answer": "Deep answer."}))]))
    r = client.post(f"/api/workspaces/{wid}/chat/async",
                    json={"message": "now go deep", "chat_id": chat_id})
    assert r.status_code == 202, r.text
    assert r.json()["chat_id"] == chat_id
    await _wait_for_job(db, r.json()["job_id"])

    detail = client.get(f"/api/workspaces/{wid}/chats/{chat_id}").json()
    assert [t["role"] for t in detail["transcript"]] == [
        "user", "assistant", "user", "assistant"]
    assert detail["transcript"][-1]["text"] == "Deep answer."


async def test_async_run_records_failure(client, db):
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]

    def boom(_record):
        from connect.llm.provider import LLMError
        raise LLMError("provider down")

    _set_llm(client, MockProvider(tool_turns=boom))
    r = client.post(f"/api/workspaces/{wid}/chat/async",
                    json={"message": "do it"})
    assert r.status_code == 202
    job = await _wait_for_job(db, r.json()["job_id"])
    assert job["status"] == "failed"
    # no assistant turn was appended on failure (only the user message)
    detail = client.get(
        f"/api/workspaces/{wid}/chats/{r.json()['chat_id']}").json()
    assert [t["role"] for t in detail["transcript"]] == ["user"]


def test_async_run_validation(client):
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("final_answer", {"answer": "x"}))]))
    # empty message -> 422
    assert client.post(f"/api/workspaces/{wid}/chat/async",
                       json={"message": "   "}).status_code == 422
    # missing workspace -> 404
    assert client.post("/api/workspaces/999999/chat/async",
                       json={"message": "hi"}).status_code == 404


def test_async_run_keyless_503(client):
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    _set_llm(client, None)
    assert client.post(f"/api/workspaces/{wid}/chat/async",
                       json={"message": "hi"}).status_code == 503


async def test_cancel_async_run(client, db):
    await insert_doc(db, title="RBI repo", text="The RBI held the repo rate.")
    wid = client.post("/api/workspaces",
                      json={"name": "W", "query_fts": "repo"}).json()["id"]
    _set_llm(client, MockProvider(tool_turns=[
        tool_turn(("final_answer", {"answer": "done"}))]))
    r = client.post(f"/api/workspaces/{wid}/chat/async",
                    json={"message": "go"})
    chat_id, job_id = r.json()["chat_id"], r.json()["job_id"]
    await _wait_for_job(db, job_id)
    # cancel is a no-op on a terminal run but still 202s with the job id
    c = client.post(f"/api/workspaces/{wid}/chats/{chat_id}/cancel")
    assert c.status_code == 202, c.text
    assert c.json()["job_id"] == job_id
