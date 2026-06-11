"""Phase 3 API surfaces: POST/GET /api/analyses (decomposition persisted,
verify writeback, verdict_summary), SSE replay ordering + ?after= resume,
cancel, and the keyless 503. MockProvider only — no network."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from fastapi.testclient import TestClient
from kb_factories import insert_doc, insert_source
from mock_llm import MockProvider

from connect.analysis.schema import (
    ClaimReasoning,
    DecomposedClaim,
    NormalizedInput,
    StanceJudgment,
)
from connect.api.main import create_app


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


def wait_for_dossier(conn, dossier_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute("SELECT status, error FROM dossier WHERE id=?",
                           (dossier_id,)).fetchone()
        if row and row["status"] in ("completed", "failed", "cancelled"):
            return row
        time.sleep(0.02)
    raise AssertionError(f"dossier {dossier_id} did not finish: {dict(row)}")


NORMALIZED = NormalizedInput(
    subject="RBI repo rate decision", input_kind="news_claim",
    claims=[
        DecomposedClaim(id="C1", text="The RBI raised the repo rate",
                        kind="factual", checkable=True),
        DecomposedClaim(id="C2", text="The RBI should cut rates instead",
                        kind="normative", checkable=False),
    ],
    seed_queries=["rbi repo rate hike"])


def stance_responder(user_text: str) -> StanceJudgment:
    if "did not raise" in user_text:
        return StanceJudgment(
            stance="refutes",
            quoted_span="the RBI did not raise the repo rate this month",
            relevance=1.0, note="explicit denial")
    if "basis points" in user_text:
        return StanceJudgment(
            stance="supports",
            quoted_span="The RBI raised the repo rate by 25 basis points",
            relevance=1.0, note="confirms the hike")
    if "curb inflation" in user_text:
        return StanceJudgment(
            stance="supports",
            quoted_span="the RBI raised the repo rate to curb inflation",
            relevance=1.0, note="confirms")
    return StanceJudgment(stance="unrelated", quoted_span="",
                          relevance=0.0, note="")


def make_provider() -> MockProvider:
    return MockProvider(respond_by_schema={
        NormalizedInput: NORMALIZED,
        StanceJudgment: stance_responder,
        ClaimReasoning: ClaimReasoning(
            reasoning="Official and press sources confirm the hike [E1].",
            cited_evidence_ids=["E1"]),
    })


def seed_corpus(conn) -> dict[str, int]:
    pib = insert_source(conn, "PIB", tier=1)
    et = insert_source(conn, "ET", tier=2)
    scroll = insert_source(conn, "Scroll", tier=2)
    d1 = insert_doc(conn, title="RBI raises repo rate", source_id=pib,
                    text="The RBI raised the repo rate by 25 basis points "
                         "on Friday, the central bank announced.")
    d2 = insert_doc(conn, title="Repo rate hiked", source_id=et,
                    text="Officials said the RBI raised the repo rate to "
                         "curb inflation across markets.")
    d3 = insert_doc(conn, title="Denial report", source_id=scroll,
                    text="Contrary to reports, the RBI did not raise the "
                         "repo rate this month, raised repo rate talk "
                         "notwithstanding.")
    return {"d1": d1, "d2": d2, "d3": d3}


def run_analysis(client, container) -> tuple[int, int]:
    container.analysis.provider = make_provider()
    res = client.post("/api/analyses", json={
        "input_text": "RBI raised the repo rate and should cut it"})
    assert res.status_code == 202
    body = res.json()
    wait_for_dossier(container.db, body["analysis_id"])
    return body["analysis_id"], body["job_id"]


# --- create / detail / list ----------------------------------------------------------------

def test_analysis_happy_path(env):
    client, container = env
    docs = seed_corpus(container.db)
    analysis_id, job_id = run_analysis(client, container)

    detail = client.get(f"/api/analyses/{analysis_id}").json()
    assert detail["status"] == "completed"
    assert detail["error"] is None
    assert detail["started_at"] and detail["finished_at"]
    assert detail["last_seq"] > 0

    # three completed stages in pipeline order, each with timing + summary
    stages = {s["stage"]: s for s in detail["stages"]}
    assert [s["stage"] for s in detail["stages"]] == [
        "normalize", "verify", "assemble"]
    assert all(s["status"] == "completed" for s in detail["stages"])
    assert stages["normalize"]["summary"] == \
        "2 claims (1 checkable), kind news_claim"
    assert "corpus-only: TAVILY_API_KEY not set" in \
        stages["verify"]["summary"]
    assert "supported 1" in stages["assemble"]["summary"]
    assert all(s["started_at"] and s["finished_at"]
               for s in detail["stages"])

    # decomposition persisted: both claims, honest checkable flags,
    # canonical integer claim ids
    claims = detail["claims"]
    assert len(claims) == 2
    checkable = claims[0]
    normative = claims[1]
    assert checkable["text"] == "The RBI raised the repo rate"
    assert checkable["checkable"] is True
    assert isinstance(checkable["id"], int)
    assert normative["checkable"] is False
    assert normative["kind"] == "normative"
    assert normative["verdict"] is None
    assert normative["evidence"] == []

    # verdict from the deterministic formula: 1.0 + 0.8 support vs 0.8
    # refute -> S = 0.3846 -> supported
    assert checkable["verdict"] == "supported"
    assert checkable["confidence"] is not None
    assert checkable["reasoning"].startswith("Official and press")
    evidence = {e["document_id"]: e for e in checkable["evidence"]}
    assert set(evidence) == {docs["d1"], docs["d2"], docs["d3"]}
    assert evidence[docs["d1"]]["stance"] == "supports"
    assert evidence[docs["d1"]]["credibility_tier"] == 1
    assert evidence[docs["d1"]]["source_name"] == "PIB"
    assert evidence[docs["d3"]]["stance"] == "refutes"
    assert "did not raise" in evidence[docs["d3"]]["quote"]

    # KB writeback: canonical claim verdict + history + grade-2 evidence
    conn = container.db
    claim_row = conn.execute("SELECT verdict, confidence FROM claim"
                             " WHERE id=?", (checkable["id"],)).fetchone()
    assert claim_row["verdict"] == "supported"
    history = conn.execute(
        'SELECT verdict, "trigger", evidence_snapshot FROM verdict_history'
        " WHERE claim_id=?", (checkable["id"],)).fetchall()
    assert [h["verdict"] for h in history] == ["supported"]
    assert history[0]["trigger"] == f"analysis:{analysis_id}"
    assert len(json.loads(history[0]["evidence_snapshot"])) == 3
    assert conn.execute("SELECT COUNT(*) FROM evidence WHERE claim_id=?"
                        " AND grade=2", (checkable["id"],)).fetchone()[0] == 3

    # contradiction scan ran after the evidence inserts
    k = conn.execute("SELECT n_support, n_refute, status FROM contradiction"
                     " WHERE claim_id=?", (checkable["id"],)).fetchone()
    assert (k["n_support"], k["n_refute"], k["status"]) == (2, 1, "open")

    # spend was ledgered under the analysis purpose
    assert conn.execute("SELECT COUNT(*) FROM llm_call WHERE"
                        " purpose='analysis'").fetchone()[0] > 0

    # list endpoint carries the verdict_summary
    page = client.get("/api/analyses").json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["id"] == analysis_id
    assert item["status"] == "completed"
    assert item["verdict_summary"] == {
        "supported": 1, "refuted": 0, "mixed": 0, "unverified": 0}


def test_analysis_validation_and_404s(env):
    client, container = env
    container.analysis.provider = make_provider()
    assert client.post("/api/analyses",
                       json={"input_text": "   "}).status_code == 422
    assert client.get("/api/analyses/999").status_code == 404
    assert client.post("/api/analyses/999/cancel").status_code == 404
    assert client.get("/api/analyses/999/events").status_code == 404


def test_analysis_keyless_503(env):
    client, container = env
    container.analysis.provider = None  # keyless deployment
    res = client.post("/api/analyses", json={"input_text": "x"})
    assert res.status_code == 503


def test_analysis_options_cap_k(env):
    client, container = env
    seed_corpus(container.db)
    container.analysis.provider = make_provider()
    res = client.post("/api/analyses", json={
        "input_text": "RBI", "options": {"max_evidence_per_claim": 1}})
    assert res.status_code == 202
    analysis_id = res.json()["analysis_id"]
    wait_for_dossier(container.db, analysis_id)
    detail = client.get(f"/api/analyses/{analysis_id}").json()
    checkable = [c for c in detail["claims"] if c["checkable"]][0]
    assert len(checkable["evidence"]) == 1  # K capped the evidence pool


# --- SSE ------------------------------------------------------------------------------------

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
    seed_corpus(container.db)
    analysis_id, _job_id = run_analysis(client, container)

    with client.stream("GET",
                       f"/api/analyses/{analysis_id}/events") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = parse_sse("".join(res.iter_text()))

    # ids are job_event.seq: strictly increasing
    ids = [e[0] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    names = [e[1] for e in events]
    # stage lifecycle ordering + terminal done{}
    assert names[0] == "stage_started"
    assert events[0][2] == {"stage": "normalize"}
    def pos(name, data_stage):
        return next(i for i, (_, n, d) in enumerate(events)
                    if n == name and d.get("stage") == data_stage)
    assert pos("stage_started", "normalize") < \
        pos("stage_completed", "normalize") < \
        pos("stage_started", "verify") < \
        pos("stage_completed", "verify") < \
        pos("stage_started", "assemble") < \
        pos("stage_completed", "assemble")
    assert names[-1] == "done"
    assert events[-1][2] == {}
    verified = [e for e in events if e[1] == "claim_verified"]
    assert len(verified) == 1
    assert verified[0][2]["verdict"] == "supported"
    assert pos("stage_started", "verify") < ids.index(verified[0][0]) \
        < pos("stage_completed", "verify")
    # 'started' (JobRunner row) is not part of the contract stream
    assert "started" not in names

    # ?after=<seq> resumes mid-stream: only later events replay
    middle = events[len(events) // 2][0]
    with client.stream(
            "GET",
            f"/api/analyses/{analysis_id}/events?after={middle}") as res:
        resumed = parse_sse("".join(res.iter_text()))
    assert [e[0] for e in resumed] == [i for i in ids if i > middle]

    # after everything -> nothing replays once terminal status is seen
    last = events[-1][0]
    with client.stream(
            "GET",
            f"/api/analyses/{analysis_id}/events?after={last}") as res:
        tail = parse_sse("".join(res.iter_text()))
    assert tail == []


def test_sse_error_event_on_failure(env):
    client, container = env
    # provider with no responders -> normalize raises -> analysis fails
    container.analysis.provider = MockProvider(respond_by_schema={})
    res = client.post("/api/analyses", json={"input_text": "x"})
    analysis_id = res.json()["analysis_id"]
    row = wait_for_dossier(container.db, analysis_id)
    assert row["status"] == "failed"
    detail = client.get(f"/api/analyses/{analysis_id}").json()
    assert detail["status"] == "failed"
    assert detail["error"]
    stages = {s["stage"]: s for s in detail["stages"]}
    assert stages["normalize"]["status"] == "failed"
    with client.stream("GET",
                       f"/api/analyses/{analysis_id}/events") as stream:
        events = parse_sse("".join(stream.iter_text()))
    assert events[-1][1] == "error"
    assert "message" in events[-1][2]


# --- cancel -----------------------------------------------------------------------------------

class HangingProvider(MockProvider):
    async def complete_structured(self, **kwargs):
        await asyncio.sleep(30)
        return await super().complete_structured(**kwargs)  # pragma: no cover


def test_cancel_running_analysis(env):
    client, container = env
    container.analysis.provider = HangingProvider()
    res = client.post("/api/analyses", json={"input_text": "x"})
    analysis_id = res.json()["analysis_id"]
    job_id = res.json()["job_id"]
    conn = container.db

    # wait until the job is actually running (normalize stage started)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = conn.execute("SELECT status FROM dossier WHERE id=?",
                           (analysis_id,)).fetchone()
        if row["status"] == "running":
            break
        time.sleep(0.02)
    assert row["status"] == "running"

    assert client.post(
        f"/api/analyses/{analysis_id}/cancel").status_code == 202
    row = wait_for_dossier(conn, analysis_id)
    assert row["status"] == "cancelled"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = conn.execute("SELECT status FROM job WHERE id=?",
                           (job_id,)).fetchone()
        if job["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert job["status"] == "cancelled"

    detail = client.get(f"/api/analyses/{analysis_id}").json()
    assert detail["status"] == "cancelled"
    # the SSE stream ends with the cancelled -> error translation
    with client.stream("GET",
                       f"/api/analyses/{analysis_id}/events") as stream:
        events = parse_sse("".join(stream.iter_text()))
    assert events[-1] [1] == "error"
    assert events[-1][2] == {"message": "analysis cancelled"}

    # cancelling a terminal analysis is a 202 no-op
    assert client.post(
        f"/api/analyses/{analysis_id}/cancel").status_code == 202
