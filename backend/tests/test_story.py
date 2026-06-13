"""Story mode — grounded narrative synthesis over a fact-set (Phase A: story
thread + investigation sources). Runs on the embedded job queue; MockProvider
returns a scripted StoryOutput, no network."""

from __future__ import annotations

import asyncio
import time

from kb_factories import insert_dossier, insert_doc, insert_source
from mock_llm import MockProvider

from connect.domain.models import DocumentAnswer
from connect.story.schema import StoryOutput
from connect.story.service import StoryService
from connect.storage.pg import utc_now
from dbutil import q1


def _set_story_llm(client, provider) -> None:
    """Point the API + worker at a StoryService backed by ``provider`` (the
    registry resolves ctx.services.stories, same object the router uses)."""
    c = client.app.state.container
    c.llm = provider
    c.stories = StoryService(c.pool, jobs=c.jobs, provider=provider,
                             governor=c.governor, embedder=c.embedder,
                             vectors=c.vectors)


def _story_provider(narrative_md: str, title: str = "The story") -> MockProvider:
    return MockProvider(respond_by_schema={
        StoryOutput: lambda _user: StoryOutput(title=title,
                                               narrative_md=narrative_md)})


async def _wait_for_job(db, job_id: int, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = await q1(db, "SELECT status, error FROM job WHERE id = %s",
                       job_id)
        if row and row["status"] in ("done", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: "
                         f"{dict(row) if row else None}")


async def _seed_investigation_with_finding(db) -> int:
    src = await insert_source(db, "RBI", tier=1)
    doc = await insert_doc(db, title="RBI holds repo rate", source_id=src,
                           text="The RBI kept the repo rate at 6.5%.")
    dossier_id = await insert_dossier(
        db, kind="investigation", input_text="Why did the RBI hold rates?",
        input_type="topic", status="completed", visibility="shared")
    cur = await db.execute(
        "INSERT INTO finding (dossier_id, kind, text, speculation,"
        " created_at) VALUES (%s,'context',%s,false,%s) RETURNING id",
        (dossier_id, "The RBI held the repo rate steady.", utc_now()))
    finding_id = int((await cur.fetchone())["id"])
    await db.execute(
        "INSERT INTO finding_evidence (finding_id, document_id, quote,"
        " quote_start, quote_end) VALUES (%s,%s,%s,0,34)",
        (finding_id, doc, "The RBI kept the repo rate at 6.5%."))
    return dossier_id


async def _seed_story_thread(db) -> int:
    src = await insert_source(db, "PIB", tier=1)
    doc = await insert_doc(db, title="GST overhaul announced", source_id=src,
                           text="The council approved a two-slab GST.")
    now = utc_now()
    cur = await db.execute(
        "INSERT INTO story (title, status, doc_count, created_at, updated_at)"
        " VALUES ('GST overhaul', 'active', 1, %s, %s) RETURNING id",
        (now, now))
    story_id = int((await cur.fetchone())["id"])
    cur = await db.execute(
        "INSERT INTO event (title, event_type, story_id, occurred_on,"
        " doc_count, created_at) VALUES ('GST council meets', 'other', %s,"
        " %s, 1, %s) RETURNING id", (story_id, now[:10], now))
    event_id = int((await cur.fetchone())["id"])
    await db.execute(
        "INSERT INTO event_assignment (document_id, event_id, method,"
        " created_at) VALUES (%s,%s,'new',%s)", (doc, event_id, now))
    return story_id


async def test_story_from_investigation(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    _set_story_llm(client, _story_provider(
        "## What happened\nThe RBI held rates [[E1]].\n## Why it happened\n"
        "This suggests caution [[E1]]."))

    r = client.post("/api/stories", json={"investigation_id": inv_id})
    assert r.status_code == 202, r.text
    story_id, job_id = r.json()["story_id"], r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"

    detail = client.get(f"/api/stories/{story_id}").json()
    assert detail["status"] == "completed"
    assert detail["title"] == "The story"
    assert detail["input_type"] == "investigation"
    assert "[[E1]]" in detail["narrative_md"]
    assert detail["grounding"]["menu_size"] == 1
    assert detail["grounding"]["cited_count"] == 1
    assert detail["sources"][0]["ref"] == "E1"
    assert detail["sources"][0]["finding_id"] is not None

    sse = client.get(f"/api/stories/{story_id}/events").text
    assert "event: done" in sse


async def test_story_from_story_thread(client, db):
    story_src_id = await _seed_story_thread(db)
    _set_story_llm(client, _story_provider(
        "## What happened\nThe GST council approved a two-slab structure"
        " [[E1]]."))

    r = client.post("/api/stories", json={"story_id": story_src_id})
    assert r.status_code == 202, r.text
    story_id, job_id = r.json()["story_id"], r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"

    detail = client.get(f"/api/stories/{story_id}").json()
    assert detail["status"] == "completed"
    assert detail["subject"] == "GST overhaul"
    assert detail["sources"][0]["document_id"] is not None
    assert detail["sources"][0]["quote"]


async def test_story_strips_unknown_citation(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    # cites E9, which is not in the 1-item menu — regenerate (still bad) then
    # strip + flag.
    _set_story_llm(client, _story_provider(
        "## What happened\nSomething happened [[E9]]."))

    r = client.post("/api/stories", json={"investigation_id": inv_id})
    job_id = r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"
    detail = client.get(f"/api/stories/{r.json()['story_id']}").json()
    assert "[[E9]]" not in detail["narrative_md"]
    assert detail["grounding"]["stripped_markers"] == ["E9"]
    assert detail["grounding"]["regenerated"] is True


async def test_story_from_workspace(client, db):
    src = await insert_source(db, "ET", tier=2)
    await insert_doc(db, title="Repo rate held", source_id=src,
                     text="The RBI kept the repo rate unchanged at 6.5%.")
    wid = client.post("/api/workspaces",
                      json={"name": "Rates desk",
                            "query_fts": "repo rate"}).json()["id"]
    _set_story_llm(client, _story_provider(
        "## What happened\nThe repo rate was held [[E1]]."))

    r = client.post("/api/stories", json={"workspace_id": wid})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"
    detail = client.get(f"/api/stories/{r.json()['story_id']}").json()
    assert detail["status"] == "completed"
    assert detail["input_type"] == "workspace"
    assert detail["subject"] == "Rates desk"
    assert detail["grounding"]["menu_size"] >= 1


async def test_story_from_topic(client, db):
    src = await insert_source(db, "LiveMint", tier=2)
    await insert_doc(db, title="Rupee slides",
                     source_id=src,
                     text="The rupee depreciated sharply against the dollar.")
    _set_story_llm(client, _story_provider(
        "## What happened\nThe rupee weakened [[E1]]."))

    r = client.post("/api/stories",
                    json={"topic": "rupee depreciation against the dollar"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"
    detail = client.get(f"/api/stories/{r.json()['story_id']}").json()
    assert detail["status"] == "completed"
    assert detail["input_type"] == "topic"
    assert detail["grounding"]["menu_size"] >= 1


async def test_story_style_reaches_prompt(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    provider = _story_provider("## What happened\nThe RBI held rates [[E1]].")
    _set_story_llm(client, provider)
    r = client.post("/api/stories", json={
        "investigation_id": inv_id,
        "options": {"style": "tight newswire voice"}})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"
    structured = [c for c in provider.calls if c.get("kind") == "structured"]
    assert any("tight newswire voice" in c["user_text"] for c in structured)


async def test_story_edit(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    _set_story_llm(client, _story_provider("## What happened\nx [[E1]]."))
    r = client.post("/api/stories", json={"investigation_id": inv_id})
    sid = r.json()["story_id"]
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"

    e = client.patch(f"/api/stories/{sid}", json={
        "title": "My edited title", "narrative_md": "## Edited\nMy own text."})
    assert e.status_code == 200, e.text
    body = e.json()
    assert body["title"] == "My edited title"
    assert body["narrative_md"] == "## Edited\nMy own text."
    assert body["grounding"]["edited"] is True


async def test_story_ask(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    provider = MockProvider(respond_by_schema={
        StoryOutput: lambda _u: StoryOutput(
            title="t", narrative_md="## What happened\nx [[E1]]."),
        DocumentAnswer: lambda _u: DocumentAnswer(
            answer="The RBI held the repo rate.", grounded=True,
            quote="The RBI kept the repo rate at 6.5%."),
    })
    _set_story_llm(client, provider)
    r = client.post("/api/stories", json={"investigation_id": inv_id})
    sid = r.json()["story_id"]
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"

    a = client.post(f"/api/stories/{sid}/ask",
                    json={"question": "What did the RBI do?"})
    assert a.status_code == 200, a.text
    assert a.json()["grounded"] is True
    assert "RBI" in a.json()["answer"]


def test_story_validation(client):
    _set_story_llm(client, _story_provider("## What happened\nx [[E1]]"))
    # no source -> 422
    assert client.post("/api/stories", json={}).status_code == 422
    # two sources -> 422
    assert client.post("/api/stories",
                       json={"story_id": 1, "topic": "x"}).status_code == 422
    # unknown investigation -> 404
    assert client.post("/api/stories",
                       json={"investigation_id": 999999}).status_code == 404


def test_story_keyless_503(client):
    c = client.app.state.container
    c.llm = None
    c.stories = StoryService(c.pool, jobs=c.jobs, provider=None,
                             governor=c.governor)
    assert client.post("/api/stories",
                       json={"topic": "anything"}).status_code == 503
