"""Social content pipeline — campaigns -> grounded multi-format drafts ->
review queue -> scheduled publish. Runs on the embedded job queue; a
MockProvider returns scripted per-format outputs, no network. Publishing is
exercised both unconfigured (real NotConnected path) and via a monkeypatched
adapter (the success path), so nothing touches a live social API.
"""

from __future__ import annotations

import asyncio
import time

from kb_factories import insert_doc, insert_source
from mock_llm import MockProvider

from connect.content.schema import (
    CarouselContent,
    CarouselSlide,
    LinkedInContent,
    MemeContent,
    ReelContent,
    ReelScene,
    ThreadContent,
)
from connect.analysis.entailment import EntailmentJudgment
from connect.content.plan import EditorialPlan, FormatPick
from connect.content.service import ContentService
from connect.domain.models import SocialPost
from connect.storage import content_items as item_dao
from connect.storage.pg import Jsonb, utc_now
from connect.workers.beat import tick
from dbutil import q1


ALL_FORMATS = ["ig_card", "ig_carousel", "x_thread", "linkedin_post"]


def _set_content_llm(client, provider) -> None:
    """Point the API + worker at a ContentService backed by ``provider`` (the
    registry resolves ctx.services.content, the same object the router uses)."""
    c = client.app.state.container
    c.llm = provider
    c.content = ContentService(
        c.pool, jobs=c.jobs, provider=provider, governor=c.governor,
        settings=c.settings, card_store=c.card_store, embedder=c.embedder,
        reel_store=c.reel_store, vectors=c.vectors)


def _content_provider(cite: str = "E1") -> MockProvider:
    m = f"[[{cite}]]"
    return MockProvider(respond_by_schema={
        # the S2 editorial gate may entailment-check numeric sentences; default
        # to 'entailed' so generation tests aren't gated on figure coverage.
        EntailmentJudgment: lambda _u: EntailmentJudgment(label="entailed"),
        SocialPost: lambda _u: SocialPost(
            headline=f"RBI holds repo {m}",
            caption=f"The RBI held the repo rate {m}. Source: RBI.",
            hashtags=["RBI", "India"], key_points=[f"Repo steady at 6.5% {m}"],
            source_label="Source: RBI", alt_text="card"),
        CarouselContent: lambda _u: CarouselContent(
            title=f"RBI holds the line {m}",
            slides=[CarouselSlide(heading="Repo unchanged",
                                  bullets=[f"Held at 6.5% {m}"]),
                    CarouselSlide(heading="Why", bullets=[f"Caution {m}"])],
            caption="A quick look. Source: RBI.", hashtags=["RBI"],
            source_label="Source: RBI", alt_text="alt"),
        ThreadContent: lambda _u: ThreadContent(
            tweets=[f"The RBI held the repo rate at 6.5% {m}.",
                    "Source: RBI"], hashtags=["RBI"]),
        LinkedInContent: lambda _u: LinkedInContent(
            body=f"The RBI held the repo rate {m}. Source: RBI.",
            hashtags=["RBI"]),
        ReelContent: lambda _u: ReelContent(
            title=f"The RBI just blinked {m}",
            image_query="Reserve Bank India building",
            scenes=[ReelScene(narration=f"The RBI held the repo rate {m}.",
                              on_screen_caption="Repo unchanged",
                              bullets=[f"Held at 6.5% {m}"],
                              image_query="Indian rupee cash"),
                    ReelScene(narration=f"Inflation guided the caution {m}.",
                              on_screen_caption="Why: inflation", bullets=[],
                              image_query="vegetable market India")],
            caption="A quick look. Source: RBI.", hashtags=["RBI"],
            source_label="Source: RBI", alt_text="alt"),
        MemeContent: lambda _u: MemeContent(
            image_query="reserve bank building",
            top_text="RBI meeting for hours",
            bottom_text=f"Repo rate: still 6.5% {m}",
            caption=f"The RBI held the repo rate {m}. Source: RBI.",
            hashtags=["RBI"], source_label="Source: RBI", alt_text="meme"),
    })


def _fake_photo_bytes() -> bytes:
    """A tiny in-memory JPEG standing in for a fetched stock photo."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), (40, 90, 140)).save(buf, format="JPEG")
    return buf.getvalue()


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
    cur = await db.execute(
        "INSERT INTO dossier (kind, input_text, input_type, status,"
        " created_at, owner_id, visibility) VALUES ('investigation',"
        " 'Why did the RBI hold rates?', 'topic', 'completed', %s,"
        " (SELECT id FROM app_user ORDER BY id LIMIT 1), 'shared')"
        " RETURNING id", (utc_now(),))
    dossier_id = int((await cur.fetchone())["id"])
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


# === generation ===============================================================


async def test_campaign_generates_all_formats(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    _set_content_llm(client, _content_provider())

    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": ALL_FORMATS})
    assert r.status_code == 202, r.text
    campaign_id, job_id = r.json()["campaign_id"], r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"

    detail = client.get(f"/api/campaigns/{campaign_id}").json()
    assert detail["status"] == "completed"
    assert detail["input_type"] == "investigation"
    items = {it["format"]: it for it in detail["items"]}
    assert set(items) == set(ALL_FORMATS)

    for fmt, it in items.items():
        assert it["status"] == "draft"
        assert it["sources"] and it["sources"][0]["ref"] == "E1"
        assert it["grounding"]["cited_count"] == 1
        # markers stripped from the published payload
        assert "[[" not in str(it["content"])
    # platforms map correctly
    assert items["ig_card"]["platform"] == "instagram"
    assert items["x_thread"]["platform"] == "x"
    assert items["linkedin_post"]["platform"] == "linkedin"
    # image formats rendered cards; text formats did not
    assert len(items["ig_card"]["card_shas"]) == 1
    assert len(items["ig_carousel"]["card_shas"]) == 3   # cover + 2 slides
    assert items["x_thread"]["card_shas"] == []

    # the public card endpoint serves a rendered carousel slide
    sha = items["ig_carousel"]["card_shas"][0]
    card = client.get(f"/api/social/card/{sha}.jpg")
    assert card.status_code == 200 and card.headers["content-type"] == \
        "image/jpeg"

    # SSE replays a terminal done
    sse = client.get(f"/api/campaigns/{campaign_id}/events").text
    assert "event: done" in sse


async def test_campaign_auto_plans_formats(client, db):
    """formats=[] hands format choice to the editorial planner: the plan is
    persisted on the campaign, the commissioned formats replace the empty
    list, the items match the picks, and the media_query steers generation."""
    inv_id = await _seed_investigation_with_finding(db)
    prov = _content_provider()
    prov.respond_by_schema[EditorialPlan] = lambda _u: EditorialPlan(
        significance=4, rationale="major rate decision",
        angle="fourth straight hold",
        picks=[FormatPick(format="ig_card", reason="feed",
                          media="stock_photo", media_query="rupee cash"),
               FormatPick(format="x_thread", reason="fast", media="none")])
    _set_content_llm(client, prov)

    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": []})
    assert r.status_code == 202, r.text
    campaign_id, job_id = r.json()["campaign_id"], r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"

    detail = client.get(f"/api/campaigns/{campaign_id}").json()
    assert detail["status"] == "completed"
    assert detail["formats"] == ["ig_card", "x_thread"]
    assert detail["plan"]["significance"] == 4
    assert detail["plan"]["big_news"] is True
    assert detail["plan"]["significance_label"] == "major"
    assert {it["format"] for it in detail["items"]} == {"ig_card", "x_thread"}

    # the planner's media_query, angle and per-pick reason all reached the
    # card generation prompt
    card_calls = [c for c in prov.calls if c.get("schema") is SocialPost]
    assert card_calls and "rupee cash" in card_calls[0]["user_text"]
    assert "fourth straight hold" in card_calls[0]["user_text"]
    assert "Why this format was commissioned: feed" in card_calls[0]["user_text"]
    # a media='none' pick gets the angle but no visual-direction line
    thread_calls = [c for c in prov.calls if c.get("schema") is ThreadContent]
    assert thread_calls and "fourth straight hold" in thread_calls[0]["user_text"]
    assert "Editorial visual direction" not in thread_calls[0]["user_text"]

    # the plan step streamed as a section event
    sse = client.get(f"/api/campaigns/{campaign_id}/events").text
    assert '"section": "plan"' in sse or '"section":"plan"' in sse


async def test_campaign_auto_honors_no_media_pick(client, db):
    """A pick with media='none' (the clean themed treatment) is HONORED: even
    when the generation model fills image_query, the stored item ships with
    it cleared so no stock photo is fetched — the render matches the plan."""
    inv_id = await _seed_investigation_with_finding(db)
    prov = _content_provider()
    prov.respond_by_schema[SocialPost] = lambda _u: SocialPost(
        headline="RBI holds repo [[E1]]",
        caption="The RBI held the repo rate [[E1]]. Source: RBI.",
        hashtags=["RBI"], key_points=["Repo steady [[E1]]"],
        source_label="Source: RBI", alt_text="card",
        image_query="rupee note")
    prov.respond_by_schema[EditorialPlan] = lambda _u: EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="clean card",
                          media="none")])
    _set_content_llm(client, prov)

    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": []})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"

    detail = client.get(f"/api/campaigns/{r.json()['campaign_id']}").json()
    item = next(it for it in detail["items"] if it["format"] == "ig_card")
    assert item["content"]["image_query"] == ""


async def test_campaign_generates_reel(client, db, monkeypatch):
    """ig_reel runs the full pipeline: grounded ReelContent -> ffmpeg MP4 ->
    item with the mp4 sha in card_shas -> served at the public reel endpoint.
    Uses the tiny/silent TEST_PROFILE so the render is fast and needs no TTS."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)

    inv_id = await _seed_investigation_with_finding(db)
    _set_content_llm(client, _content_provider())
    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": ["ig_reel"]})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"

    detail = client.get(f"/api/campaigns/{r.json()['campaign_id']}").json()
    item = detail["items"][0]
    assert item["format"] == "ig_reel"
    assert item["platform"] == "instagram"
    assert item["status"] == "draft"
    assert item["grounding"]["cited_count"] == 1
    assert "[[" not in str(item["content"])     # markers stripped
    assert len(item["card_shas"]) == 1          # the mp4 sha

    sha = item["card_shas"][0]
    vid = client.get(f"/api/social/reel/{sha}.mp4")
    assert vid.status_code == 200
    assert vid.headers["content-type"] == "video/mp4"
    assert vid.content[4:8] == b"ftyp"          # a real MP4


async def test_campaign_generates_meme(client, db, monkeypatch):
    """meme runs the full pipeline: grounded MemeContent -> stock photo
    (monkeypatched — no network) -> impact-caption JPEG in card_shas ->
    served at the public card endpoint."""
    import connect.content.imagery as imagery_mod
    calls: list[str] = []

    def fake_fetch(query, *, settings=None, variant=0):
        calls.append(query)
        return _fake_photo_bytes()

    monkeypatch.setattr(imagery_mod, "fetch_scene_image", fake_fetch)

    inv_id = await _seed_investigation_with_finding(db)
    _set_content_llm(client, _content_provider())
    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": ["meme"]})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert (await _wait_for_job(db, job_id))["status"] == "done"

    detail = client.get(f"/api/campaigns/{r.json()['campaign_id']}").json()
    item = detail["items"][0]
    assert item["format"] == "meme"
    assert item["platform"] == "instagram"
    assert item["status"] == "draft"
    assert item["grounding"]["cited_count"] == 1
    assert "[[" not in str(item["content"])     # markers stripped
    assert item["content"]["top_text"] == "RBI meeting for hours"
    assert len(item["card_shas"]) == 1
    assert calls == ["reserve bank building"]   # the meme's photo was fetched

    sha = item["card_shas"][0]
    card = client.get(f"/api/social/card/{sha}.jpg")
    assert card.status_code == 200
    assert card.headers["content-type"] == "image/jpeg"


def test_render_meme_offline():
    """render_meme is pure: photo bytes in -> JPEG out; no photo -> themed
    fallback; junk photo bytes -> themed fallback (never raises)."""
    from connect.content.render import render_meme
    from connect.domain.models import PostSettings

    meme = MemeContent(
        image_query="crowded train", top_text="Setup line",
        bottom_text="Punchline", caption="The real news. Source: X.",
        hashtags=[], source_label="Source: X")
    s = PostSettings()
    with_photo = render_meme(meme, settings=s, photo=_fake_photo_bytes())
    plain = render_meme(meme, settings=s, photo=None)
    junk = render_meme(meme, settings=s, photo=b"not an image")
    for jpeg in (with_photo, plain, junk):
        assert jpeg[:3] == b"\xff\xd8\xff"      # JPEG magic
    assert with_photo != plain


def test_render_card_and_carousel_photo_background():
    """A photo becomes a scrimmed background on cards / carousel slides; the
    plain render is unchanged when no photo is given."""
    from connect.content.render import render_carousel
    from connect.social.card import render_card
    from connect.domain.models import PostSettings

    s = PostSettings()
    post = SocialPost(headline="Inflation eases", caption="c",
                      key_points=["Food prices fell"],
                      source_label="Source: RBI")
    assert render_card(post, settings=s, photo=_fake_photo_bytes()) != \
        render_card(post, settings=s)

    car = CarouselContent(
        title="Cover", image_query="market",
        slides=[CarouselSlide(heading="One", bullets=["a"],
                              image_query="veg"),
                CarouselSlide(heading="Two", bullets=["b"])],
        caption="c", hashtags=[], source_label="Source: X")
    photod = render_carousel(car, settings=s,
                             photos=[_fake_photo_bytes(), None, None])
    plain = render_carousel(car, settings=s)
    assert len(photod) == len(plain) == 3
    assert photod[0] != plain[0]                # cover got the photo
    assert photod[2] == plain[2]                # un-photoed slide unchanged


def test_zapier_payload_for_meme():
    from connect.content.publish import build_zapier_payload

    payload = build_zapier_payload(
        "meme",
        {"image_query": "q", "top_text": "T", "bottom_text": "B",
         "caption": "The news. Source: X.", "hashtags": ["News"]},
        ["http://x/card/abc.jpg"], item_id=7)
    assert payload["platform"] == "instagram"
    assert payload["media_type"] == "image"
    assert payload["caption"].startswith("The news.")
    assert "#News" in payload["caption"]


async def test_rerender_reel_after_edit(client, db, monkeypatch):
    """Edit a reel's script + render controls, re-render, and the stored video
    refreshes. Uses TEST_PROFILE (silent, no images) + engine 'none' so it's
    hermetic."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)

    item_id = await _one_item(client, db, fmt="ig_reel")
    item = client.get(f"/api/content/{item_id}").json()
    edited = dict(item["content"])
    edited["title"] = "An edited hook"

    r = client.post(f"/api/content/{item_id}/rerender",
                    json={"content": edited,
                          "options": {"music": False, "tts_engine": "none"}})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"

    after = client.get(f"/api/content/{item_id}").json()
    assert after["edited"] is True
    assert after["content"]["title"] == "An edited hook"   # script edit persisted
    assert len(after["card_shas"]) == 1                    # video re-rendered
    sha = after["card_shas"][0]
    assert client.get(f"/api/social/reel/{sha}.mp4").status_code == 200


def test_render_reel_produces_mp4():
    """The renderer turns a ReelContent into a real MP4 (no DB, no network)."""
    from connect.content.reel import TEST_PROFILE, render_reel
    from connect.domain.models import PostSettings

    content = ReelContent(
        title="RBI holds the line", image_query="bank building",
        scenes=[ReelScene(narration="The RBI held the repo rate at 6.5%.",
                          on_screen_caption="Repo unchanged", bullets=["5th pause"],
                          image_query="rupee cash"),
                ReelScene(narration="Inflation guided the caution.",
                          on_screen_caption="Why: inflation", bullets=[],
                          image_query="market")],
        caption="A quick look.", hashtags=[], source_label="Source: RBI",
        alt_text="alt")
    mp4 = render_reel(content, settings=PostSettings(), logo=None,
                      tts_settings=None, profile=TEST_PROFILE)
    assert mp4[4:8] == b"ftyp"
    assert len(mp4) > 1000


async def test_campaign_repurposes_a_story(client, db):
    """story_dossier_id reads the story's persisted 'scope' fact-set back."""
    src = await insert_source(db, "PIB", tier=1)
    doc = await insert_doc(db, title="GST overhaul", source_id=src,
                           text="The council approved a two-slab GST.")
    cur = await db.execute(
        "INSERT INTO dossier (kind, title, input_text, input_type, status,"
        " created_at, owner_id, visibility) VALUES ('story', 'GST overhaul',"
        " 'GST overhaul', 'topic', 'completed', %s,"
        " (SELECT id FROM app_user ORDER BY id LIMIT 1), 'shared')"
        " RETURNING id", (utc_now(),))
    story_id = int((await cur.fetchone())["id"])
    facts = {"subject": "GST overhaul", "facts": [{
        "document_id": doc, "quote": "The council approved a two-slab GST.",
        "title": "GST overhaul", "source_name": "PIB",
        "credibility_tier": 1, "occurred_on": None, "finding_id": None}]}
    await db.execute(
        "INSERT INTO dossier_section (dossier_id, stage, status, content,"
        " created_at) VALUES (%s,'scope','completed',%s,%s)",
        (story_id, Jsonb(facts), utc_now()))

    _set_content_llm(client, _content_provider())
    r = client.post("/api/campaigns",
                    json={"story_dossier_id": story_id, "formats": ["ig_card"]})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"
    detail = client.get(f"/api/campaigns/{r.json()['campaign_id']}").json()
    assert detail["input_type"] == "story"
    assert detail["items"][0]["sources"][0]["document_id"] == doc


async def test_campaign_auto_theme_carries_palette(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    prov = MockProvider(respond_by_schema={
        SocialPost: lambda _u: SocialPost(
            headline="RBI holds repo [[E1]]",
            caption="The RBI held rates [[E1]]. Source: RBI.",
            hashtags=["RBI"], key_points=["Repo steady at 6.5% [[E1]]"],
            source_label="Source: RBI", alt_text="a",
            suggested_palette="crimson")})
    _set_content_llm(client, prov)
    client.put("/api/social/settings", json={"auto_theme": True})

    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": ["ig_card"]})
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"
    item = client.get(
        f"/api/campaigns/{r.json()['campaign_id']}").json()["items"][0]
    # the AI palette is preserved through grounding-strip + persisted, and a
    # themed card was rendered
    assert item["content"]["suggested_palette"] == "crimson"
    assert len(item["card_shas"]) == 1


async def test_generation_strips_unknown_citation(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    _set_content_llm(client, _content_provider(cite="E9"))   # not on menu
    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": ["ig_card"]})
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"
    item = client.get(
        f"/api/campaigns/{r.json()['campaign_id']}").json()["items"][0]
    assert item["grounding"]["regenerated"] is True
    assert item["grounding"]["stripped_markers"] == ["E9"]
    assert "[[E9]]" not in str(item["content"])


# === review queue =============================================================


async def _one_item(client, db, fmt: str = "ig_card") -> int:
    inv_id = await _seed_investigation_with_finding(db)
    _set_content_llm(client, _content_provider())
    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": [fmt]})
    await _wait_for_job(db, r.json()["job_id"])
    detail = client.get(f"/api/campaigns/{r.json()['campaign_id']}").json()
    return detail["items"][0]["id"]


async def test_review_queue_transitions(client, db):
    item_id = await _one_item(client, db)

    # approve: draft -> approved
    r = client.post(f"/api/content/{item_id}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"

    # schedule: -> scheduled with the chosen time
    r = client.post(f"/api/content/{item_id}/schedule",
                    json={"scheduled_at": "2030-01-01T09:00:00Z"})
    assert r.status_code == 200 and r.json()["status"] == "scheduled"
    assert r.json()["scheduled_at"].startswith("2030-01-01")

    # invalid timestamp -> 422
    assert client.post(f"/api/content/{item_id}/schedule",
                       json={"scheduled_at": "not-a-date"}).status_code == 422

    # hand-edit flags edited
    r = client.patch(f"/api/content/{item_id}",
                     json={"content": {"headline": "Edited", "caption": "c",
                                       "hashtags": [], "key_points": [],
                                       "source_label": "", "alt_text": ""}})
    assert r.status_code == 200 and r.json()["edited"] is True
    assert r.json()["content"]["headline"] == "Edited"

    # reject
    r = client.post(f"/api/content/{item_id}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    # the queue filters by status
    page = client.get("/api/content?status=rejected").json()
    assert any(it["id"] == item_id for it in page["items"])

    # unknown item -> 404
    assert client.post("/api/content/999999/approve").status_code == 404


async def test_delete_campaign_removes_items(client, db):
    inv_id = await _seed_investigation_with_finding(db)
    _set_content_llm(client, _content_provider())
    r = client.post("/api/campaigns",
                    json={"investigation_id": inv_id, "formats": ["ig_card"]})
    cid = r.json()["campaign_id"]
    await _wait_for_job(db, r.json()["job_id"])
    item_id = client.get(f"/api/campaigns/{cid}").json()["items"][0]["id"]

    # delete -> 204; the campaign and its items (FK cascade) are gone
    assert client.delete(f"/api/campaigns/{cid}").status_code == 204
    assert client.get(f"/api/campaigns/{cid}").status_code == 404
    assert client.get(f"/api/content/{item_id}").status_code == 404

    # deleting a missing campaign -> 404
    assert client.delete(f"/api/campaigns/{cid}").status_code == 404


async def test_campaigns_filter_by_workspace(client, db):
    _set_content_llm(client, _content_provider())
    wid = client.post("/api/workspaces", json={"name": "WS content"}).json()["id"]
    other = client.post("/api/workspaces", json={"name": "Other WS"}).json()["id"]

    ws_cid = client.post(
        "/api/campaigns",
        json={"workspace_id": wid, "formats": ["ig_card"]},
    ).json()["campaign_id"]

    # scoping returns only this workspace's campaign
    scoped = client.get("/api/campaigns", params={"workspace_id": wid}).json()
    assert [c["id"] for c in scoped["items"]] == [ws_cid]
    assert scoped["total"] == 1

    # a different workspace sees none of it
    assert client.get("/api/campaigns",
                      params={"workspace_id": other}).json()["total"] == 0


async def test_platforms_status(client):
    body = client.get("/api/content/platforms").json()
    assert body == {"instagram": False, "x": False, "linkedin": False}


# === publish ==================================================================


async def test_publish_handler_success(client, db, monkeypatch):
    item_id = await _one_item(client, db, fmt="ig_card")
    client.post(f"/api/content/{item_id}/approve")
    c = client.app.state.container
    c.settings.public_base_url = "https://connect.example.com"

    async def _fake(settings, *, fmt, content, card_urls):
        assert card_urls and card_urls[0].startswith("https://connect")
        return {"media_id": "ig-123", "permalink": "https://insta/p/123"}

    monkeypatch.setattr("connect.content.publish.publish_item", _fake)
    result = await c.content.publish_item(db, item_id)
    assert result == "published"
    item = await item_dao.get(db, item_id, viewer=1)
    assert item.status == "published"
    assert item.publish_ref["media_id"] == "ig-123"


async def test_publish_reel_url_built(client, db, monkeypatch):
    """A reel item publishes via the /api/social/reel/<sha>.mp4 video URL."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)
    item_id = await _one_item(client, db, fmt="ig_reel")
    client.post(f"/api/content/{item_id}/approve")
    c = client.app.state.container
    c.settings.public_base_url = "https://connect.example.com"

    async def _fake(settings, *, fmt, content, card_urls):
        assert fmt == "ig_reel"
        assert card_urls and card_urls[0].endswith(".mp4")
        assert "/api/social/reel/" in card_urls[0]
        return {"media_id": "reel-1", "permalink": "https://insta/reel/1"}

    monkeypatch.setattr("connect.content.publish.publish_item", _fake)
    assert await c.content.publish_item(db, item_id) == "published"
    item = await item_dao.get(db, item_id, viewer=1)
    assert item.status == "published"
    assert item.publish_ref["media_id"] == "reel-1"


async def test_publish_via_zapier(client, db, monkeypatch):
    c = client.app.state.container
    c.settings.public_base_url = "https://connect.example.com"
    item_id = await _one_item(client, db, fmt="ig_card")
    client.post(f"/api/content/{item_id}/approve")

    # not connected: capability is false and the route refuses with 503
    c.settings.zapier_webhook_url = None
    assert client.get("/api/content/capabilities").json()["zapier"] is False
    assert client.post(f"/api/content/{item_id}/publish",
                       params={"target": "zapier"}).status_code == 503

    # connected: the webhook receives the assembled payload, item -> published
    c.settings.zapier_webhook_url = "https://hooks.zapier.com/hooks/catch/1/x"
    assert client.get("/api/content/capabilities").json()["zapier"] is True

    captured: dict = {}

    async def _fake(settings, *, payload):
        captured.update(payload)
        return {"media_id": "zap-1", "permalink": None, "via": "zapier"}

    monkeypatch.setattr("connect.content.platforms.zapier.publish", _fake)
    assert await c.content.publish_item(db, item_id, target="zapier") == "published"
    assert captured["format"] == "ig_card"
    assert captured["platform"] == "instagram"
    assert captured["media_type"] == "image"
    assert captured["media_urls"][0].startswith("https://connect")
    assert "#" in captured["caption"]  # hashtags folded into the caption
    item = await item_dao.get(db, item_id, viewer=1)
    assert item.status == "published" and item.publish_ref["via"] == "zapier"


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeIGClient:
    """Scripts the IG Reels container flow: create -> poll status -> publish."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.status_polls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, **kw):
        return _FakeResp({"id": "m1" if url.endswith("/media_publish")
                          else "c1"})

    async def get(self, url, params=None, **kw):
        if "status_code" in (params or {}).get("fields", ""):
            i = min(self.status_polls, len(self.statuses) - 1)
            self.status_polls += 1
            return _FakeResp({"status_code": self.statuses[i]})
        return _FakeResp({"permalink": "https://insta/reel/1"})


async def test_publish_reel_polls_until_finished(monkeypatch):
    from types import SimpleNamespace

    from connect.social import instagram

    fake = _FakeIGClient(["IN_PROGRESS", "FINISHED"])
    monkeypatch.setattr(instagram.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.setattr(instagram, "REEL_POLL_INTERVAL", 0.0)
    settings = SimpleNamespace(instagram_access_token="t",
                               instagram_business_account_id="ig",
                               instagram_reel_timeout_s=5.0)
    res = await instagram.publish_reel(settings, video_url="https://x/r.mp4",
                                       caption="hi")
    assert res["media_id"] == "m1"
    assert res["permalink"] == "https://insta/reel/1"
    assert fake.status_polls >= 2     # polled past the IN_PROGRESS state


async def test_publish_reel_times_out(monkeypatch):
    import pytest
    from types import SimpleNamespace

    from connect.social import instagram

    fake = _FakeIGClient(["IN_PROGRESS"])   # never FINISHED
    monkeypatch.setattr(instagram.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.setattr(instagram, "REEL_POLL_INTERVAL", 0.0)
    settings = SimpleNamespace(instagram_access_token="t",
                               instagram_business_account_id="ig",
                               instagram_reel_timeout_s=-1.0)  # deadline in past
    with pytest.raises(instagram.InstagramError, match="timed out"):
        await instagram.publish_reel(settings, video_url="https://x/r.mp4",
                                     caption="hi")


async def test_publish_unconfigured_fails_loudly(client, db):
    item_id = await _one_item(client, db, fmt="x_thread")
    client.post(f"/api/content/{item_id}/approve")
    c = client.app.state.container
    result = await c.content.publish_item(db, item_id)
    assert result == "failed"
    item = await item_dao.get(db, item_id, viewer=1)
    assert item.status == "failed"
    assert "not connected" in (item.error or "").lower()


async def test_beat_enqueues_due_content(client, db):
    item_id = await _one_item(client, db, fmt="x_thread")
    client.post(f"/api/content/{item_id}/approve")
    # schedule in the past so the due-scan picks it up immediately
    client.post(f"/api/content/{item_id}/schedule",
                json={"scheduled_at": "2020-01-01T00:00:00Z"})

    assert item_id in await item_dao.list_due(db)

    # guard the nightly steps so the tick only does the due-content scan
    for task in ("enrich_t1_batch", "brief_pregen"):
        await db.execute(
            "INSERT INTO beat_run (task, last_run_at) VALUES (%s,"
            " '9999-12-31T00:00:00.000Z') ON CONFLICT (task) DO UPDATE"
            " SET last_run_at = EXCLUDED.last_run_at", (task,))

    counts = await tick(client.app.state.container)
    assert counts["publishes"] >= 1

    # the embedded queue ran the publish job; x is unconfigured -> failed
    row = await q1(db, "SELECT id FROM job WHERE kind = 'content_publish'"
                       " AND payload->>'item_id' = %s", str(item_id))
    assert row is not None
    await _wait_for_job(db, int(row["id"]))
    item = await item_dao.get(db, item_id, viewer=1)
    assert item.status == "failed"
