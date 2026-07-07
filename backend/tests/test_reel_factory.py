"""The reel factory — topic scout (deterministic heat SQL + LLM assignment
editor), the reel script editor (critique -> revise, grounding preserved),
the reel_factory job end-to-end through the API, and the beat schedule.
MockProvider everywhere; the one real render uses the tiny TEST_PROFILE.
"""

from __future__ import annotations

import asyncio
import time

from kb_factories import ensure_user, insert_doc, insert_source
from mock_llm import MockProvider

from connect.analysis.entailment import EntailmentJudgment
from connect.content import scout
from connect.content.edit import ReelCritique
from connect.content.schema import ReelContent, ReelScene
from connect.content.scout import _Assignments, _Pick
from connect.content.service import ContentService
from connect.content.factory import ReelFactoryService
from connect.storage.pg import Jsonb, utc_now
from connect.workers.beat import tick
from dbutil import q1, qall


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


# === corpus seeding ===========================================================


async def _seed_thread(db, *, title="Rupee slides to a record low",
                       n_docs=3) -> int:
    """An active story thread with one fresh event backed by ``n_docs``
    documents from distinct tier-1 sources. Returns the story id."""
    cur = await db.execute(
        "INSERT INTO story (title, status, doc_count, created_at)"
        " VALUES (%s, 'active', %s, %s) RETURNING id",
        (title, n_docs, utc_now()))
    story_id = int((await cur.fetchone())["id"])
    cur = await db.execute(
        "INSERT INTO event (title, event_type, story_id, occurred_on,"
        " doc_count, last_seen_at, created_at)"
        " VALUES (%s, 'market_move', %s, CURRENT_DATE, %s, CURRENT_DATE, %s)"
        " RETURNING id", (title, story_id, n_docs, utc_now()))
    event_id = int((await cur.fetchone())["id"])
    for i in range(n_docs):
        src = await insert_source(db, f"Outlet {story_id}-{i}", tier=1)
        doc = await insert_doc(
            db, title=f"{title} — report {i}", source_id=src,
            text=f"The rupee fell to a record low of 90.{i} per dollar.")
        await db.execute(
            "INSERT INTO event_assignment (event_id, document_id, method,"
            " created_at) VALUES (%s,%s,'attach',%s)",
            (event_id, doc, utc_now()))
    return story_id


async def _seed_unthreaded_event(db, *, title="Fuel prices cut by Rs 5",
                                 n_docs=2) -> int:
    cur = await db.execute(
        "INSERT INTO event (title, event_type, occurred_on, doc_count,"
        " last_seen_at, created_at)"
        " VALUES (%s, 'policy_change', CURRENT_DATE, %s, CURRENT_DATE, %s)"
        " RETURNING id", (title, n_docs, utc_now()))
    event_id = int((await cur.fetchone())["id"])
    for i in range(n_docs):
        src = await insert_source(db, f"EvOutlet {event_id}-{i}", tier=2)
        doc = await insert_doc(db, title=f"{title} {i}", source_id=src,
                               text=f"Fuel prices were cut by Rs 5 ({i}).")
        await db.execute(
            "INSERT INTO event_assignment (event_id, document_id, method,"
            " created_at) VALUES (%s,%s,'attach',%s)",
            (event_id, doc, utc_now()))
    return event_id


async def _seed_hot_topic(db, topic="taxation", n_docs=2) -> None:
    for i in range(n_docs):
        src = await insert_source(db, f"TopicOutlet {topic}-{i}", tier=2)
        doc = await insert_doc(db, title=f"GST update {i}", source_id=src,
                               text=f"A GST change was announced ({i}).")
        await db.execute(
            "INSERT INTO document_topic (document_id, topic, source)"
            " VALUES (%s,%s,'t1')", (doc, topic))


# === the scout ================================================================


async def test_scout_candidates_ranks_and_dedups(db):
    owner = await ensure_user(db)
    story_id = await _seed_thread(db)
    await _seed_unthreaded_event(db)
    await _seed_hot_topic(db)

    cands = await scout.scout_candidates(db, window_hours=24, dedup_days=3)
    kinds = [c.kind for c in cands]
    assert kinds[0] == "thread"                 # threads outrank the rest
    assert "event" in kinds and "topic" in kinds
    thread = cands[0]
    assert thread.story_id == story_id
    assert thread.heat == 3                     # 3 distinct tier-1 outlets

    # a campaign covering the thread's subject drops it from the next slate
    await db.execute(
        "INSERT INTO campaign (owner_id, subject, input_type, seed, formats,"
        " options, status, visibility, created_at)"
        " VALUES (%s, %s, 'topic', %s, %s, %s, 'completed', 'shared', %s)",
        (owner, thread.subject, Jsonb({}), Jsonb([]), Jsonb({}), utc_now()))
    cands2 = await scout.scout_candidates(db, window_hours=24, dedup_days=3)
    assert all(c.subject != thread.subject for c in cands2)


async def test_pick_assignments_without_provider_takes_slate_top(db, pool):
    from connect.analysis.budget import AnalysisBudget

    await _seed_thread(db)
    await _seed_unthreaded_event(db)
    cands = await scout.scout_candidates(db, window_hours=24)
    picks = await scout.pick_assignments(
        db, None, candidates=cands, count=2, window_hours=24,
        budget=AnalysisBudget(0.1), governor=None, viewer=1)
    assert [p.candidate.subject for p in picks] == \
        [c.subject for c in cands[:2]]


async def test_pick_assignments_maps_and_bounds_llm_picks(db):
    from connect.analysis.budget import AnalysisBudget

    viewer = await ensure_user(db)
    await _seed_thread(db)
    await _seed_unthreaded_event(db)
    cands = await scout.scout_candidates(db, window_hours=24)
    assert len(cands) >= 2
    provider = MockProvider(respond_by_schema={
        _Assignments: lambda _u: _Assignments(picks=[
            _Pick(candidate=99, angle="out of range", reason="bad"),
            _Pick(candidate=2, angle="Rs 5 off overnight [[E1]]",
                  reason="relatable pocketbook story"),
            _Pick(candidate=2, angle="dup", reason="dup"),
        ])})

    class _Gov:
        async def check(self, projected, user_id=None):
            return None

    picks = await scout.pick_assignments(
        db, provider, candidates=cands, count=2, window_hours=24,
        budget=AnalysisBudget(0.1), governor=_Gov(), viewer=viewer)
    # 99 dropped, the dup dropped, the one valid pick survives —
    # citation-marker-ish tokens are scrubbed from free text
    assert len(picks) == 1
    assert picks[0].candidate.subject == cands[1].subject
    assert "E1" not in picks[0].angle and "Rs 5" in picks[0].angle


# === the script editor ========================================================


def _reel(title: str, cite: str = "E1") -> ReelContent:
    m = f"[[{cite}]]"
    return ReelContent(
        title=title, image_query="rupee cash",
        scenes=[ReelScene(narration=f"The rupee hit a record low {m}.",
                          on_screen_caption="Record low", bullets=[],
                          image_query="currency exchange board"),
                ReelScene(narration=f"Three outlets confirm the slide {m}.",
                          on_screen_caption="Confirmed", bullets=[],
                          image_query="Mumbai stock exchange")],
        caption="The rupee story. Source: PTI.", hashtags=["rupee"],
        source_label="Source: PTI", alt_text="reel")


def _factory_provider(*, critiques, reels) -> MockProvider:
    """A provider whose ReelCritique / ReelContent responses pop off lists
    (the last entry repeats), plus the fixed scout/gate responses."""
    state = {"crit": 0, "reel": 0}

    def _crit(_u):
        i = min(state["crit"], len(critiques) - 1)
        state["crit"] += 1
        return critiques[i]

    def _reel_pop(_u):
        i = min(state["reel"], len(reels) - 1)
        state["reel"] += 1
        return reels[i]

    return MockProvider(respond_by_schema={
        EntailmentJudgment: lambda _u: EntailmentJudgment(label="entailed"),
        _Assignments: lambda _u: _Assignments(picks=[
            _Pick(candidate=1, angle="the record-low number first",
                  reason="market-moving, corroborated")]),
        ReelCritique: _crit,
        ReelContent: _reel_pop,
    })


def _set_factory_llm(client, provider) -> None:
    """Rewire content + factory services around ``provider`` (the registry
    resolves ctx.services.*, the same objects the routers use)."""
    c = client.app.state.container
    c.llm = provider
    c.content = ContentService(
        c.pool, jobs=c.jobs, provider=provider, governor=c.governor,
        settings=c.settings, card_store=c.card_store, embedder=c.embedder,
        reel_store=c.reel_store, vectors=c.vectors)
    c.reel_factory = ReelFactoryService(
        c.pool, jobs=c.jobs, provider=provider, governor=c.governor,
        settings=c.settings, content=c.content)


async def test_editor_revises_weak_script(client, db, monkeypatch):
    """A 'revise' critique triggers a rewrite; the shipped item carries the
    revised script and the editor report in its grounding."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)

    await _seed_thread(db)
    provider = _factory_provider(
        critiques=[ReelCritique(hook=2, pacing=4, visuals=4, clarity=4, retention=3,
                                verdict="revise",
                                notes=["lead with the 90.0 number"]),
                   ReelCritique(hook=5, pacing=5, visuals=5, clarity=5, retention=5,
                                verdict="ship", notes=[])],
        reels=[_reel("The rupee moved today"),        # weak draft
               _reel("90 to the dollar. It happened")])  # revision
    _set_factory_llm(client, provider)

    r = client.post("/api/factory/reels", json={"count": 1})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"

    runs = client.get("/api/factory/runs").json()["items"]
    assert runs and runs[0]["campaigns"]
    cjob = runs[0]["campaigns"][0]["job_id"]
    assert (await _wait_for_job(db, cjob))["status"] == "done"

    detail = client.get(
        f"/api/campaigns/{runs[0]['campaigns'][0]['campaign_id']}").json()
    item = detail["items"][0]
    assert item["format"] == "ig_reel"
    assert item["content"]["title"].startswith("90 to the dollar")
    editor = item["grounding"]["editor"]
    assert editor["revised"] is True
    assert [p["verdict"] for p in editor["passes"]] == ["revise", "ship"]
    assert len(item["card_shas"]) == 1          # the rendered mp4


async def test_editor_discards_offmenu_revision(client, db, monkeypatch):
    """A revision that cites ids outside the menu (twice) is thrown away —
    the original grounded draft ships instead."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)

    await _seed_thread(db)
    provider = _factory_provider(
        critiques=[ReelCritique(hook=2, pacing=3, visuals=3, clarity=3, retention=3,
                                verdict="revise", notes=["punch it up"])],
        reels=[_reel("The rupee moved today"),        # grounded draft
               _reel("Fabricated!", cite="E99"),      # off-menu revision
               _reel("Still fabricated!", cite="E99")])
    _set_factory_llm(client, provider)

    r = client.post("/api/factory/reels", json={"count": 1})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"
    runs = client.get("/api/factory/runs").json()["items"]
    cjob = runs[0]["campaigns"][0]["job_id"]
    assert (await _wait_for_job(db, cjob))["status"] == "done"

    detail = client.get(
        f"/api/campaigns/{runs[0]['campaigns'][0]['campaign_id']}").json()
    item = detail["items"][0]
    assert item["content"]["title"] == "The rupee moved today"
    editor = item["grounding"]["editor"]
    assert editor["revised"] is False
    assert editor["passes"][0].get("revision_discarded") is True


# === the factory run ==========================================================


async def test_factory_run_end_to_end(client, db, monkeypatch):
    """POST /api/factory/reels: scout -> assignment -> one reel-led campaign
    whose item renders a real (tiny) MP4; the run surface reports it all."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)

    story_id = await _seed_thread(db)
    provider = _factory_provider(
        critiques=[ReelCritique(hook=5, pacing=5, visuals=5, clarity=5, retention=5,
                                verdict="ship", notes=[])],
        reels=[_reel("90 to the dollar. It happened")])
    _set_factory_llm(client, provider)

    r = client.post("/api/factory/reels", json={"count": 1})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"

    runs = client.get("/api/factory/runs").json()["items"]
    assert runs[0]["status"] == "done"
    assert runs[0]["candidates"] >= 1
    assert runs[0]["assignments"][0]["kind"] == "thread"
    assert runs[0]["campaigns"], "the run should have created a campaign"

    campaign_id = runs[0]["campaigns"][0]["campaign_id"]
    cjob = runs[0]["campaigns"][0]["job_id"]
    assert (await _wait_for_job(db, cjob))["status"] == "done"

    detail = client.get(f"/api/campaigns/{campaign_id}").json()
    assert detail["status"] == "completed"
    assert detail["formats"] == ["ig_reel"]
    # the thread pick seeds from the story (event-ordered facts)
    row = await q1(db, "SELECT seed FROM campaign WHERE id = %s", campaign_id)
    assert row["seed"]["story_id"] == story_id
    item = detail["items"][0]
    assert item["format"] == "ig_reel" and item["status"] == "draft"
    sha = item["card_shas"][0]
    vid = client.get(f"/api/social/reel/{sha}.mp4")
    assert vid.status_code == 200
    assert vid.content[4:8] == b"ftyp"          # a real MP4

    # a second run immediately after: the covered subject is off the slate
    r2 = client.post("/api/factory/reels", json={"count": 1})
    assert r2.status_code == 202
    assert (await _wait_for_job(db, r2.json()["job_id"]))["status"] == "done"
    runs2 = client.get("/api/factory/runs").json()["items"]
    latest = runs2[0]
    assert all(a["subject"] != "Rupee slides to a record low"
               for a in latest["assignments"])


async def test_factory_events_stream_shape(client, db, monkeypatch):
    """The run's job_event log carries scouted/assignment/campaign_created —
    what the SSE endpoint replays and /runs reconstructs."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)

    await _seed_thread(db)
    provider = _factory_provider(
        critiques=[ReelCritique(hook=5, pacing=5, visuals=5, clarity=5, retention=5,
                                verdict="ship", notes=[])],
        reels=[_reel("90 to the dollar. It happened")])
    _set_factory_llm(client, provider)

    r = client.post("/api/factory/reels", json={"count": 1})
    job_id = r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"
    types = [row["type"] for row in await qall(
        db, "SELECT type FROM job_event WHERE job_id = %s ORDER BY seq",
        job_id)]
    assert types[0] == "started" and types[-1] == "done"
    assert "scouted" in types and "assignment" in types \
        and "campaign_created" in types


# === render quality guards ====================================================


def test_broll_title_filter_rejects_unrelated_clips():
    """Commons search matches file DESCRIPTIONS, landing vlogs and agency
    slates behind news scenes — a clip qualifies only when its filename
    shares (almost) the whole query, precision over recall: a rejected clip
    falls back to the reliably on-subject photo path."""
    from connect.content.video import _title_matches

    q = "Pakistani shopkeeper adjusting price labels"
    assert _title_matches(
        "File:Shopkeeper adjusting price labels, Karachi.webm", q)
    # two shared words are not enough — 'Indian district court building'
    # word-matches every US '...District Court...' hearing upload
    assert not _title_matches(
        "File:Judge Smith US District Court hearing.webm",
        "Indian district court building")
    assert not _title_matches("File:Pakistan bazaar 2019.webm", q)
    assert not _title_matches("File:My day at the beach vlog.webm", q)
    # short queries require every content word
    assert _title_matches("File:Effigy burning at protest.webm",
                          "protest effigy")
    assert not _title_matches("File:Street protest Delhi.webm",
                              "protest effigy")
    # a query with no usable content words filters nothing
    assert _title_matches("File:Anything at all.webm", "a of in")


def test_karaoke_chunks_fit_the_frame():
    """Caption chunks must stay short enough that the burned karaoke style
    never overflows 1080px ('PAKISTAN'S GOVERNMENT' clipped at 22 chars)."""
    from connect.content.reel import _chunk_words

    words = [(w, 0.0, 0.1) for w in
             "While ten percent inflation squeezes Pakistani families the"
             " government presented an eighteen trillion rupee budget"
             .split()]
    chunks = _chunk_words(words)
    assert all(
        len(" ".join(w[0] for w in chunk)) <= 17 for chunk in chunks)
    assert [w[0] for chunk in chunks for w in chunk] == [w[0] for w in words]


def test_rotate_grouped_never_promotes_looping_clips():
    """Variant rotation diversifies adjacent scenes WITHIN each covers-group
    — it must never put a too-short (looping) clip ahead of one that plays
    the scene through."""
    from connect.content.video import _rotate_grouped

    cands = [(0, "a"), (0, "b"), (1, "x"), (1, "y"), (1, "z")]
    assert _rotate_grouped(cands, 0) == ["a", "b", "x", "y", "z"]
    r1 = _rotate_grouped(cands, 1)
    assert r1[:2] == ["b", "a"]           # rotated, but covers-0 still first
    assert set(r1[2:]) == {"x", "y", "z"}
    assert _rotate_grouped([], 3) == []


def test_scene_pad_matches_xfade():
    """The narration/caption timeline only lines up with the xfaded video
    timeline when the per-scene pad equals the crossfade overlap — a gap
    drifts audio ahead of picture by (pad - xfade) per scene."""
    from connect.content import reel

    assert reel._SCENE_PAD == reel._XFADE


async def test_factory_run_is_singleton(db):
    """Two enqueues while a run is live dedup to the SAME job — concurrent
    runs would scout the same slate and double the spend."""
    from connect.storage import jobs as job_dao

    owner = await ensure_user(db)
    a = await job_dao.create(db, "reel_factory", {"owner_id": owner},
                             owner_id=owner)
    b = await job_dao.create(db, "reel_factory", {"owner_id": owner},
                             owner_id=owner)
    assert a == b
    await db.execute("UPDATE job SET status = 'done' WHERE id = %s", (a,))
    c = await job_dao.create(db, "reel_factory", {"owner_id": owner},
                             owner_id=owner)
    assert c != a                          # a finished run frees the slot


async def test_private_run_hidden_from_other_viewers(client, db):
    """A run started with visibility='private' commissions campaigns the
    campaign API hides from other users — its run record (subjects,
    campaign ids, errors) must be hidden the same way."""
    c = client.app.state.container
    owner = await ensure_user(db, "owner-priv@test.local")
    other = await ensure_user(db, "other@test.local", role="member")
    await db.execute(
        "INSERT INTO job (kind, priority, payload, status, max_attempts,"
        " created_at, owner_id) VALUES ('reel_factory', 50, %s, 'done', 1,"
        " %s, %s)",
        (Jsonb({"visibility": "private", "owner_id": owner}), utc_now(),
         owner))
    await db.execute(
        "INSERT INTO job (kind, priority, payload, status, max_attempts,"
        " created_at, owner_id) VALUES ('reel_factory', 50, %s, 'done', 1,"
        " %s, %s)",
        (Jsonb({"visibility": "shared", "owner_id": owner}), utc_now(),
         owner))
    mine = await c.reel_factory.list_runs(db, viewer=owner)
    theirs = await c.reel_factory.list_runs(db, viewer=other)
    assert len(mine) - len(theirs) == 1    # the private run is owner-only


def test_asset_cache_roundtrip_prune_and_off(tmp_path):
    """Fetched assets persist to disk (fetch once, reuse across processes),
    the size cap prunes oldest-first, and asset_cache_mb=0 disables it."""
    import time
    from types import SimpleNamespace

    from connect.content import asset_cache

    s = SimpleNamespace(blob_dir=tmp_path, asset_cache_mb=1)
    asset_cache.put(s, "img", "rupee|0", b"x" * 10)
    assert asset_cache.get(s, "img", "rupee|0") == b"x" * 10
    assert asset_cache.get(s, "img", "other|0") is None
    assert asset_cache.get(s, "vid", "rupee|0") is None   # kind-scoped

    # cap 1MB: two ~700KB entries -> the older one is pruned
    asset_cache.put(s, "vid", "a", b"a" * 700_000)
    time.sleep(0.02)
    asset_cache.put(s, "vid", "b", b"b" * 700_000)
    assert asset_cache.get(s, "vid", "a") is None
    assert asset_cache.get(s, "vid", "b") is not None

    off = SimpleNamespace(blob_dir=tmp_path, asset_cache_mb=0)
    asset_cache.put(off, "img", "k", b"data")
    assert asset_cache.get(off, "img", "k") is None
    assert asset_cache.get(None, "img", "k") is None       # no settings


def test_fetch_scene_image_reads_disk_cache(tmp_path, monkeypatch):
    """A cached photo is served without touching any network source — the
    'fetch once, use locally after' contract."""
    from types import SimpleNamespace

    from connect.content import asset_cache, imagery

    s = SimpleNamespace(blob_dir=tmp_path, asset_cache_mb=64,
                        pexels_api_key=None)
    asset_cache.put(s, "img", "rupee cash|1", b"photo-bytes")
    imagery._cache.clear()

    def _boom(*a, **k):
        raise AssertionError("network hit despite disk cache")

    monkeypatch.setattr(imagery, "_pexels_candidates", _boom)
    monkeypatch.setattr(imagery, "_openverse_candidates", _boom)
    monkeypatch.setattr(imagery, "_wikimedia_candidates", _boom)
    assert imagery.fetch_scene_image("rupee cash", settings=s,
                                     variant=1) == b"photo-bytes"
    imagery._cache.clear()


def test_query_broadening_includes_generic_cut():
    """News queries lead with the specific place — broadening must offer the
    place-less cut ('courthouse India'), the variant stock sites can hit."""
    from connect.content.imagery import _broaden
    from connect.content.video import _query_variants

    assert _broaden("Narmadapuram courthouse India")[:2] == [
        "Narmadapuram courthouse India", "courthouse India"]
    assert "district court building" in \
        _query_variants("Indian district court building")


# === the beat schedule ========================================================


async def test_beat_schedules_daily_factory_run(client, db):
    """With reel_factory_enabled, one run per UTC day at/after the hour —
    guarded by beat_run, owned by the first admin."""
    c = client.app.state.container
    hour = int(utc_now()[11:13])
    c.settings = c.settings.model_copy(update={
        "reel_factory_enabled": True, "reel_factory_utc_hour": hour,
        "poller_enabled": False,
        "nightly_sweep_utc_hour": (hour + 2) % 24})
    counts = await tick(c)
    assert counts["reel_factory"] == 1
    counts2 = await tick(c)                     # same day: guarded
    assert counts2["reel_factory"] == 0
    job = await q1(db, "SELECT id, owner_id, payload FROM job WHERE kind ="
                       " 'reel_factory' ORDER BY id DESC LIMIT 1")
    assert job is not None
    admin = await q1(db, "SELECT id FROM app_user WHERE role = 'admin'"
                         " ORDER BY id LIMIT 1")
    assert job["owner_id"] == admin["id"]
    assert job["payload"]["owner_id"] == admin["id"]
