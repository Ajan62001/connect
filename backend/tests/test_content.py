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
from connect.content.edit import ReelCritique
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
        # the reel script editor critiques every ig_reel draft; default to a
        # clean 'ship' so single-shot generation tests stay single-shot.
        ReelCritique: lambda _u: ReelCritique(
            hook=5, pacing=5, visuals=5, clarity=5, retention=5,
            verdict="ship", notes=[]),
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


async def test_campaign_uses_workspace_channel_persona(client, db, monkeypatch):
    """A campaign bound to a workspace channel (channel_workspace_id) writes the
    reel IN CHARACTER: the resolved persona + the workspace's tone reach the
    generation prompt, and the citation system prompt is untouched. The
    settings-ignored bug (get_global instead of effective) is fixed."""
    import connect.content.reel as reel_mod
    monkeypatch.setattr(reel_mod, "_DEFAULT_PROFILE", reel_mod.TEST_PROFILE)

    captured: dict = {}
    prov = _content_provider()
    base_reel = prov.respond_by_schema[ReelContent]

    def _reel(user_text):
        captured["reel_prompt"] = user_text
        captured["reel_system"] = None   # filled below via calls inspection
        return base_reel(user_text)
    prov.respond_by_schema[ReelContent] = _reel
    _set_content_llm(client, prov)

    # a workspace with a bound character + a distinctive brand override in its
    # post_settings (which reaches the prompt only via effective(), the fix)
    ws = client.post("/api/workspaces", json={"name": "Channel WS"}).json()
    client.patch(f"/api/workspaces/{ws['id']}",
                 json={"post_settings": {"brand_handle": "@bantuowl"}})
    ch = client.post("/api/characters", json={
        "name": "Bantu the Owl", "description": "a wise nocturnal explainer",
        "speaking_style": "hoots between sentences", "voice_id": "vb:omega",
        "sign_off": "Hoot hoot."}).json()
    client.put(f"/api/workspaces/{ws['id']}/channel",
               json={"default_character_id": ch["id"]})

    inv_id = await _seed_investigation_with_finding(db)
    r = client.post("/api/campaigns", json={
        "investigation_id": inv_id, "formats": ["ig_reel"],
        "channel_workspace_id": ws["id"]})
    assert r.status_code == 202, r.text
    assert (await _wait_for_job(db, r.json()["job_id"]))["status"] == "done"

    prompt = captured["reel_prompt"]
    assert "Bantu the Owl" in prompt              # persona reached the prompt
    assert "hoots between sentences" in prompt
    # the workspace's post_settings brand reached the prompt — proof the
    # pipeline now uses effective(workspace), not the global-only settings
    assert "@bantuowl" in prompt
    # the persona is framing-only: the citation discipline is restated
    assert "[[E#]]" in prompt
    # the reel still renders and grounds normally
    detail = client.get(f"/api/campaigns/{r.json()['campaign_id']}").json()
    assert detail["items"][0]["grounding"]["cited_count"] == 1


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


def test_fitted_photo_keeps_whole_image():
    """The default 'fitted' style contains the WHOLE photo on the solid theme
    colour (both side edges visible — nothing side-cropped), on cards and on
    reel scene backgrounds; 'cover' still crops full-bleed."""
    import io
    from PIL import Image
    from connect.content.reel import _fit_box, _scene_bg
    from connect.social.card import _Theme, render_card
    from connect.domain.models import PostSettings

    # a wide photo with a unique red border so its true edges are detectable
    edge = (250, 40, 30)
    src = Image.new("RGB", (1600, 900), (40, 90, 140))
    for xy in ([0, 0, 1599, 12], [0, 887, 1599, 899],
               [0, 0, 12, 899], [1587, 0, 1599, 899]):
        src.paste(edge, xy)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    photo = buf.getvalue()

    s = PostSettings().model_copy(update={"card_photo_style": "fitted"})
    t = _Theme(s)

    bg = _scene_bg(photo, t, (1080, 1920), style="fitted")
    assert bg.getpixel((2, 2)) == t.bg            # solid colour, not photo
    assert bg.getpixel((1077, 1917)) == t.bg
    x0, y0, x1, y1 = _fit_box((1080, 1920))
    mid = (y0 + y1) // 2
    def reddish(p):
        return p[0] > 180 and p[1] < 110 and p[2] < 110
    assert reddish(bg.getpixel((x0 + 3, mid)))    # LEFT photo edge on frame
    assert reddish(bg.getpixel((x1 - 4, mid)))    # RIGHT photo edge on frame

    covered = _scene_bg(photo, t, (1080, 1920), style="cover")
    left, right = covered.getpixel((2, 960)), covered.getpixel((1077, 960))
    assert not (reddish(left) and reddish(right))  # cover crops the sides

    # Ken-Burns safety: zoompan magnifies about the FRAME centre, so a scene
    # with max zoom 1.14 (the hook) must pre-shrink the media enough that the
    # magnified media still stays inside the design box — never under the
    # logo band above it, never into the text panel below it.
    from PIL import ImageChops
    zoomed = _scene_bg(photo, t, (1080, 1920), style="fitted", zoom=1.14)
    bbox = ImageChops.difference(
        zoomed, Image.new("RGB", zoomed.size, t.bg)).getbbox()
    assert bbox is not None
    for p, c, lo, hi in ((bbox[0], 540, x0, x1), (bbox[2], 540, x0, x1),
                         (bbox[1], 960, y0, y1), (bbox[3], 960, y0, y1)):
        assert lo - 2 <= c + (p - c) * 1.14 <= hi + 2   # at full magnification

    post = SocialPost(headline="Inflation eases", caption="c",
                      key_points=["Food prices fell"],
                      source_label="Source: RBI")
    fitted = render_card(post, settings=s, photo=photo)
    cover = render_card(post, settings=s.model_copy(
        update={"card_photo_style": "cover"}), photo=photo)
    junk = render_card(post, settings=s, photo=b"not an image")
    for jpeg in (fitted, cover, junk):
        assert jpeg[:3] == b"\xff\xd8\xff"
    assert fitted != cover
    card = Image.open(io.BytesIO(fitted)).convert("RGB")
    reds = [p for p in card.getdata() if reddish(p)]
    assert reds                                    # the photo edge survived


def test_poster_card_is_default_and_captioned():
    """'poster' (the default) renders the photo full-bleed with the headline
    in the accent colour pinned to the bottom; junk photo bytes fall back to
    the solid template; the other styles still render distinct cards."""
    import io
    from PIL import Image
    from connect.social.card import render_card
    from connect.domain.models import PostSettings

    s = PostSettings()
    assert s.card_photo_style == "poster"

    src = Image.new("RGB", (1600, 900), (40, 90, 140))
    buf = io.BytesIO()
    src.save(buf, format="JPEG")
    photo = buf.getvalue()

    post = SocialPost(
        headline="Portugal knocks out Croatia after the most dramatic 2nd half",
        caption="c", key_points=["Late winner in extra time"],
        source_label="Source: FIFA")
    poster = render_card(post, settings=s, photo=photo)
    fitted = render_card(post, settings=s.model_copy(
        update={"card_photo_style": "fitted"}), photo=photo)
    junk = render_card(post, settings=s, photo=b"junk")
    for jpeg in (poster, fitted, junk):
        assert jpeg[:3] == b"\xff\xd8\xff"
    assert poster != fitted

    # the headline renders in the accent colour in the bottom half
    img = Image.open(io.BytesIO(poster)).convert("RGB")
    from connect.social.card import _Theme
    ar, ag, ab = _Theme(s).accent
    hits = sum(
        1 for yy in range(540, 1080, 6) for xx in range(0, 1080, 6)
        if (lambda p: abs(p[0] - ar) < 40 and abs(p[1] - ag) < 40
            and abs(p[2] - ab) < 40)(img.getpixel((xx, yy))))
    assert hits > 20


def test_fitted_video_segment_pads_solid():
    """The fitted ffmpeg path scales the clip INTO the media box and pads the
    rest with the theme colour (validates the pad expression end-to-end)."""
    import io
    import subprocess
    import tempfile
    from pathlib import Path
    from PIL import Image
    from connect.content.reel import (ReelProfile, _fit_box,
                                      _render_video_segment)
    from connect.social.card import _Theme
    from connect.domain.models import PostSettings

    prof = ReelProfile(size=(270, 480), fps=12, preset="ultrafast", crf=30)
    t = _Theme(PostSettings())
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        src = work / "src.png"
        Image.new("RGB", (320, 180), (250, 40, 30)).save(src)
        clip = work / "clip.mp4"
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(src),
                        "-t", "1", "-r", "12", "-pix_fmt", "yuv420p",
                        str(clip)], check=True, capture_output=True)
        overlay = work / "ov.png"
        Image.new("RGBA", prof.size, (0, 0, 0, 0)).save(overlay)
        seg = work / "seg.mp4"
        _render_video_segment("ffmpeg", clip, overlay, 1.0, prof, seg,
                              style="fitted", bg_rgb=t.bg)
        frame = work / "frame.png"
        subprocess.run(["ffmpeg", "-y", "-i", str(seg), "-vframes", "1",
                        str(frame)], check=True, capture_output=True)
        img = Image.open(frame).convert("RGB")
        corner = img.getpixel((2, 2))
        assert all(abs(a - b) < 24 for a, b in zip(corner, t.bg))  # solid pad
        x0, y0, x1, y1 = _fit_box(prof.size)
        centre = img.getpixel(((x0 + x1) // 2, (y0 + y1) // 2))
        assert centre[0] > 180 and centre[1] < 110                # clip visible


def test_poster_reel_karaoke_matches_brand():
    """In the poster visual style (the reel default) the burned karaoke
    captions restyle to match the poster cards: accent colour, rounded font,
    sentence case, pinned low — instead of white uppercase mid-frame."""
    from connect.content.reel import _build_ass, _visual_style
    from connect.social.card import _Theme
    from connect.domain.models import PostSettings

    assert _visual_style(None) == "poster"        # the server default
    t = _Theme(PostSettings())
    scenes = [{"kind": "scene"}]
    words = [[("Ronaldo", 0.0, 0.4), ("scores", 0.4, 0.8)]]

    poster = _build_ass(scenes, [1.0], words, (1080, 1920), t=t,
                        visual_style="poster")
    r, g, b = t.accent
    assert f"&H00{b:02X}{g:02X}{r:02X}" in poster  # accent (ASS is BGR)
    assert "Ubuntu" in poster
    assert "Ronaldo scores" in poster              # sentence case kept

    plain = _build_ass(scenes, [1.0], words, (1080, 1920), t=t,
                       visual_style="cover")
    assert "&H00FFFFFF" in plain
    assert "RONALDO SCORES" in plain


def test_voicebox_engine_switchable():
    """voicebox slots in as a switchable TTS engine: 'auto' prefers ElevenLabs
    then voicebox then Piper; picking a 'vb:' voice from the studio switches
    that render's engine + profile; voicebox profiles join the voice list."""
    from types import SimpleNamespace
    from connect.content.service import ContentService
    from connect.social import tts

    # engine resolution order
    def s(**kw):
        base = {"reel_tts_engine": "auto", "elevenlabs_api_key": None,
                "voicebox_url": None, "piper_voice_dir": None}
        return SimpleNamespace(**{**base, **kw})
    assert tts._resolve_engine(s(voicebox_url="http://x")) == "voicebox"
    assert tts._resolve_engine(
        s(elevenlabs_api_key="k", voicebox_url="http://x")) == "elevenlabs"
    assert tts._resolve_engine(s()) == "none"
    assert tts._resolve_engine(
        s(reel_tts_engine="voicebox")) == "voicebox"   # explicit switch

    # a 'vb:' voice pick maps to the voicebox engine + profile
    from connect.content.schema import ContentOptions
    from connect.orchestration.config import Settings
    svc = SimpleNamespace(settings=Settings(_env_file=None))
    eff = ContentService._render_settings(
        svc, ContentOptions(voice_id="vb:abc-123"))
    assert eff.reel_tts_engine == "voicebox"
    assert eff.voicebox_profile_id == "abc-123"
    eff = ContentService._render_settings(
        svc, ContentOptions(voice_id="ELVoiceId99"))
    assert eff.reel_tts_engine == "elevenlabs"
    assert eff.elevenlabs_voice_id == "ELVoiceId99"

    # voicebox profiles appear in the combined picker list as vb: entries
    import unittest.mock as mock
    with mock.patch.object(tts, "voicebox_profiles", return_value=[
            {"id": "p1", "name": "Omega", "language": "hi",
             "description": "Kokoro preset voice (Hindi)"}]):
        voices = tts.list_voices(s(voicebox_url="http://x"))
    assert voices == [{"id": "vb:p1", "name": "Omega (voicebox)",
                       "description": "HI · Kokoro preset voice (Hindi)"}]


def test_worker_options_tolerate_version_skew():
    """A job payload written by a different build may carry option keys this
    build doesn't know (ContentOptions is extra='forbid'); the worker drops
    them instead of failing the job."""
    from connect.content.schema import ContentOptions
    from connect.workers.handlers.content import _options

    opts = _options({"visual_style": "fitted", "music": False,
                     "from_a_newer_build": 1})
    assert opts.visual_style == "fitted"
    assert opts.music is False
    assert _options(None) == ContentOptions()


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
    seen_url: dict = {}

    async def _fake(settings, *, payload, url_override=None):
        captured.update(payload)
        seen_url["url"] = url_override
        return {"media_id": "zap-1", "permalink": None, "via": "zapier"}

    monkeypatch.setattr("connect.content.platforms.zapier.publish", _fake)
    assert await c.content.publish_item(db, item_id, target="zapier") == "published"
    assert captured["format"] == "ig_card"
    assert captured["platform"] == "instagram"
    assert captured["media_type"] == "image"
    assert captured["media_urls"][0].startswith("https://connect")
    assert "#" in captured["caption"]  # hashtags folded into the caption
    # no channel bound => global webhook, no per-channel override or block
    assert seen_url["url"] is None
    assert "channel" not in captured
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
