"""The /api/investigations surface: create (202/422/404/503/429), list
counts, snapshot detail, SSE replay ordering + ?after= resume, cancel, and
POST /api/questions/{id}/investigate (manual recursion). MockProvider
scripted tool turns — no network."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from fastapi.testclient import TestClient
from kb_factories import insert_doc
from mock_llm import MockProvider, tool_turn

from connect.api.main import create_app
from connect.investigation.schema import (
    GeneratedQuestion,
    GeneratedQuestions,
    SynthesisOutput,
    WatchNextItem,
)
from connect.llm.provider import Usage

DOC_TEXT = ("The RBI tightened the rules in response to the payment fraud "
            "wave reported in May. Industry groups objected.")
QUOTE = ("The RBI tightened the rules in response to the payment fraud "
         "wave reported in May.")


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


def wait_for_dossier(conn, dossier_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = conn.execute("SELECT status, error FROM dossier WHERE id=?",
                           (dossier_id,)).fetchone()
        if row and row["status"] in ("completed", "failed", "cancelled"):
            return row
        time.sleep(0.02)
    raise AssertionError(
        f"dossier {dossier_id} did not finish: {dict(row) if row else None}")


def make_provider(doc_id: int) -> MockProvider:
    qgen = GeneratedQuestions(questions=[GeneratedQuestion(
        qtype="what_triggered", text="What triggered the tightening?",
        priority=0.9)])
    synth = SynthesisOutput(
        narrative_md="The fraud wave triggered the tightening [[f1]].",
        watch_next=[WatchNextItem(text="watch the open question",
                                  question_id=1)])

    def scripted(record):
        if record["tool_choice"] == "conclude":
            return tool_turn(("conclude", {
                "summary": "Fraud wave triggered the tightening.",
                "resolutions": [{"question_id": 1, "status": "answered",
                                 "answer_summary": "the fraud wave",
                                 "finding_ids": [1]}],
                "confidence": 0.8}))
        n = sum(1 for c in provider.calls if c.get("kind") == "tools")
        if n == 1:
            return tool_turn(("search_corpus", {"query": "rbi rules"}))
        if n == 2:
            return tool_turn(("record_finding", {
                "kind": "trigger",
                "text": "The fraud wave triggered the tightening.",
                "evidence": [{"document_id": doc_id, "quote": QUOTE}],
                "speculation": False, "question_id": 1,
                "confidence": 0.8}))
        return tool_turn(("conclude", {
            "summary": "Fraud wave triggered the tightening.",
            "resolutions": [{"question_id": 1, "status": "answered",
                             "answer_summary": "the fraud wave",
                             "finding_ids": [1]}],
            "confidence": 0.8}))

    provider = MockProvider(
        respond_by_schema={GeneratedQuestions: qgen,
                           SynthesisOutput: synth},
        tool_turns=scripted)
    return provider


def run_investigation(client, container) -> tuple[int, int]:
    doc_id = insert_doc(container.db, title="RBI tightens rules",
                        text=DOC_TEXT)
    container.investigations.provider = make_provider(doc_id)
    res = client.post("/api/investigations",
                      json={"topic": "rbi payment rules"})
    assert res.status_code == 202
    body = res.json()
    wait_for_dossier(container.db, body["investigation_id"])
    return body["investigation_id"], body["job_id"]


# --- create / validation ---------------------------------------------------------


def test_create_validation_and_404s(env):
    client, container = env
    container.investigations.provider = MockProvider()
    # no seed field / two seed fields -> 422
    assert client.post("/api/investigations", json={}).status_code == 422
    assert client.post("/api/investigations", json={
        "topic": "x", "entity_id": 1}).status_code == 422
    assert client.post("/api/investigations",
                       json={"topic": "  "}).status_code == 422
    # referenced rows must exist -> 404
    assert client.post("/api/investigations",
                       json={"entity_id": 999}).status_code == 404
    assert client.post("/api/investigations",
                       json={"event_id": 999}).status_code == 404
    assert client.post("/api/investigations",
                       json={"story_id": 999}).status_code == 404
    # unknown investigation surfaces
    assert client.get("/api/investigations/999").status_code == 404
    assert client.post("/api/investigations/999/cancel").status_code == 404
    assert client.get("/api/investigations/999/events").status_code == 404
    assert client.post(
        "/api/questions/999/investigate").status_code == 404


def test_keyless_503(env):
    client, container = env
    container.investigations.provider = None
    res = client.post("/api/investigations", json={"topic": "x"})
    assert res.status_code == 503


def test_governor_429(env):
    client, container = env
    container.investigations.provider = MockProvider()
    from connect.llm import spend
    # exhaust today's investigation envelope (~$10.2 on sonnet input)
    spend.record_call(container.db, purpose="investigation",
                      model="claude-sonnet-4-6",
                      usage=Usage(input_tokens=3_400_000))
    res = client.post("/api/investigations", json={"topic": "x"})
    assert res.status_code == 429
    # the GENERAL surface is not gated by investigation spend: an analysis
    # still starts (its governor excludes investigation purposes)
    container.analysis.provider = MockProvider()
    assert client.post("/api/analyses",
                       json={"input_text": "x"}).status_code == 202


# --- happy path: detail snapshot + list counts ---------------------------------------


def test_investigation_happy_path(env):
    client, container = env
    investigation_id, _job_id = run_investigation(client, container)
    conn = container.db

    detail = client.get(f"/api/investigations/{investigation_id}").json()
    assert detail["status"] == "completed"
    assert detail["error"] is None
    assert detail["input_type"] == "topic"
    assert detail["budget_usd"] == 1.0
    assert detail["last_seq"] > 0
    assert detail["cost_usd"] > 0

    # three stages, pipeline order, all completed
    assert [s["stage"] for s in detail["stages"]] == [
        "scope", "investigate", "synthesize"]
    assert all(s["status"] == "completed" for s in detail["stages"])
    assert "reaction candidates" in detail["stages"][0]["summary"]

    # sections: scope pack + loop summary + all six output sections
    sections = detail["sections"]
    for name in ("scope", "investigate", "timeline", "causal_narrative",
                 "actors", "alternatives", "open_questions", "watch_next"):
        assert name in sections, name
    assert sections["investigate"]["concluded"] is True
    assert sections["causal_narrative"]["narrative_md"].endswith("[[f1]].")
    assert sections["causal_narrative"]["grounding_report"][
        "stripped_citations"] == []
    assert sections["watch_next"]["items"][0]["question_id"] == 1
    assert sections["open_questions"]["items"][0]["status"] == "answered"

    # questions + findings with span-verified evidence
    assert len(detail["questions"]) == 1
    q = detail["questions"][0]
    assert (q["qtype"], q["status"]) == ("what_triggered", "answered")
    assert q["answer_finding_ids"] == [1]
    assert len(detail["findings"]) == 1
    f = detail["findings"][0]
    assert f["kind"] == "trigger" and f["speculation"] is False
    assert f["evidence"][0]["quote"] == QUOTE
    assert f["evidence"][0]["document_id"] > 0

    assert detail["counts"] == {"findings": 1, "questions_open": 0,
                                "questions_answered": 1, "docs_added": 0}

    # ledger: loop spend under 'investigation', synthesis under its own
    purposes = {r[0] for r in conn.execute(
        "SELECT DISTINCT purpose FROM llm_call")}
    assert {"investigation", "investigation_synthesis"} <= purposes

    # list endpoint carries counts
    page = client.get("/api/investigations").json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["id"] == investigation_id
    assert item["counts"]["findings"] == 1
    assert item["counts"]["questions_answered"] == 1

    # the kind split is bidirectional: the analyses surface never shows
    # investigation dossiers
    assert client.get("/api/analyses").json()["total"] == 0
    assert client.get(
        f"/api/analyses/{investigation_id}").status_code == 404


def test_web_investigation_source_seeded(env):
    _client, container = env
    row = container.db.execute(
        "SELECT type, credibility_tier, enabled FROM source"
        " WHERE name = 'Web (investigation)'").fetchone()
    assert row is not None
    assert (row["type"], row["credibility_tier"]) == ("search", 4)


# --- SSE -------------------------------------------------------------------------------


def parse_sse(text: str) -> list[tuple[int, str, dict]]:
    events = []
    for block in re.split(r"\r?\n\r?\n", text):
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields.setdefault(key.strip(), value.strip())
        if "event" in fields and fields["event"] != "ping":
            events.append((int(fields["id"]), fields["event"],
                           json.loads(fields.get("data") or "{}")))
    return events


def test_sse_replay_ordering_and_resume(env):
    client, container = env
    investigation_id, _job_id = run_investigation(client, container)

    with client.stream(
            "GET",
            f"/api/investigations/{investigation_id}/events") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = parse_sse("".join(res.iter_text()))

    ids = [e[0] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    names = [e[1] for e in events]
    assert "started" not in names          # JobRunner row is dropped
    assert names[-1] == "done"
    # contract ordering: questions exist before iterations record findings
    assert names.index("question_raised") < names.index("iteration")
    assert names.index("finding_recorded") < names.index(
        "question_resolved")
    iteration_ns = [d["n"] for _, n, d in events if n == "iteration"]
    assert iteration_ns == sorted(iteration_ns)
    sections = [d["section"] for _, n, d in events
                if n == "section_completed"]
    assert sections[0] == "scope"
    assert {"investigate", "timeline", "causal_narrative", "actors",
            "alternatives", "open_questions", "watch_next",
            "synthesize"} <= set(sections)

    # ?after= resumes mid-stream
    middle = events[len(events) // 2][0]
    with client.stream(
            "GET", f"/api/investigations/{investigation_id}"
                   f"/events?after={middle}") as res:
        resumed = parse_sse("".join(res.iter_text()))
    assert [e[0] for e in resumed] == [i for i in ids if i > middle]

    # after the terminal event nothing replays
    with client.stream(
            "GET", f"/api/investigations/{investigation_id}"
                   f"/events?after={ids[-1]}") as res:
        assert parse_sse("".join(res.iter_text())) == []


# --- cancel ------------------------------------------------------------------------------


class HangingToolsProvider(MockProvider):
    async def complete_with_tools(self, **kwargs):
        await asyncio.sleep(30)
        return await super().complete_with_tools(**kwargs)


def test_cancel_running_investigation(env):
    client, container = env
    container.investigations.provider = HangingToolsProvider(
        respond_by_schema={GeneratedQuestions: GeneratedQuestions(),
                           SynthesisOutput: SynthesisOutput(
                               narrative_md="")})
    res = client.post("/api/investigations", json={"topic": "hang"})
    assert res.status_code == 202
    investigation_id = res.json()["investigation_id"]
    conn = container.db

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = conn.execute("SELECT status FROM dossier WHERE id=?",
                           (investigation_id,)).fetchone()
        if row["status"] == "running":
            break
        time.sleep(0.02)
    assert row["status"] == "running"

    assert client.post(
        f"/api/investigations/{investigation_id}/cancel"
    ).status_code == 202
    row = wait_for_dossier(conn, investigation_id)
    assert row["status"] == "cancelled"
    with client.stream(
            "GET",
            f"/api/investigations/{investigation_id}/events") as stream:
        events = parse_sse("".join(stream.iter_text()))
    assert events[-1][1] == "error"
    assert events[-1][2] == {"message": "investigation cancelled"}
    # cancelling a terminal investigation is a 202 no-op
    assert client.post(
        f"/api/investigations/{investigation_id}/cancel"
    ).status_code == 202


# --- manual recursion ----------------------------------------------------------------------


def test_question_investigate_recursion(env):
    client, container = env
    parent_id, _job_id = run_investigation(client, container)
    conn = container.db
    question_id = conn.execute(
        "SELECT id FROM question WHERE dossier_id = ?",
        (parent_id,)).fetchone()[0]

    res = client.post(f"/api/questions/{question_id}/investigate")
    assert res.status_code == 202
    child_id = res.json()["investigation_id"]
    assert child_id != parent_id
    wait_for_dossier(conn, child_id)

    # lineage on both sides: dossier.parent_question_id and
    # question.spawned_dossier_id
    child = conn.execute("SELECT * FROM dossier WHERE id = ?",
                         (child_id,)).fetchone()
    assert child["kind"] == "investigation"
    assert child["parent_question_id"] == question_id
    assert child["input_type"] == "topic"
    assert child["input_text"] == "What triggered the tightening?"
    q = conn.execute("SELECT spawned_dossier_id FROM question"
                     " WHERE id = ?", (question_id,)).fetchone()
    assert q["spawned_dossier_id"] == child_id

    # the open_questions section of the parent exposes the spawn
    detail = client.get(f"/api/investigations/{parent_id}").json()
    assert detail["questions"][0]["spawned_dossier_id"] == child_id
