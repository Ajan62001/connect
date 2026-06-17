"""Post-generation settings — the global default (app_setting), admin update,
per-workspace overrides + effective merge, and that the social generator
threads them into the prompt + card."""

from __future__ import annotations

import io

from PIL import Image

from connect.api.routers.social import _system_for
from connect.domain.models import PostSettings
from connect.social import settings as post_settings
from connect.social.card import render_card


def test_global_settings_default(client):
    s = client.get("/api/social/settings").json()
    assert s["hashtag_count"] == 8
    assert s["card_accent"] == "#38bdf8"
    assert s["sign_off"] == "via connect"


def test_admin_update_global_settings(client):
    r = client.put("/api/social/settings",
                   json={"hashtag_count": 12, "card_accent": "#ff5500",
                         "brand_handle": "@connect"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hashtag_count"] == 12
    assert body["card_accent"] == "#ff5500"
    assert body["brand_handle"] == "@connect"
    # unspecified keys keep the default
    assert body["sign_off"] == "via connect"
    # persisted
    assert client.get("/api/social/settings").json()["hashtag_count"] == 12


def test_workspace_overrides_merge(client):
    # set a global default first
    client.put("/api/social/settings", json={"hashtag_count": 10})
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    # workspace overrides only the tone + hashtag_count
    client.patch(f"/api/workspaces/{wid}",
                 json={"post_settings": {"tone": "punchy", "hashtag_count": 3}})

    eff = client.get(f"/api/workspaces/{wid}/post-settings").json()
    assert eff["tone"] == "punchy"          # workspace override
    assert eff["hashtag_count"] == 3        # workspace override wins over global
    assert eff["sign_off"] == "via connect"  # falls through to default

    # the workspace row carries the raw overrides
    ws = client.get(f"/api/workspaces/{wid}").json()
    assert ws["post_settings"] == {"tone": "punchy", "hashtag_count": 3}


# --- resolution unit ---------------------------------------------------------

async def test_effective_merges_global_then_workspace(db):
    await post_settings.set_global(db, PostSettings(hashtag_count=9,
                                                    card_accent="#111111"))

    class _WS:
        post_settings = {"hashtag_count": 2, "tone": "bold"}

    eff = await post_settings.effective(db, _WS())
    assert eff.hashtag_count == 2        # workspace wins
    assert eff.tone == "bold"            # workspace
    assert eff.card_accent == "#111111"  # global
    # no workspace -> just global
    g = await post_settings.effective(db)
    assert g.hashtag_count == 9 and g.tone == PostSettings().tone


def test_merge_ignores_none_and_unknown_keys():
    base = PostSettings()
    merged = post_settings._merge(
        base, {"hashtag_count": None, "bogus": "x", "tone": "calm"})
    assert merged.tone == "calm"
    assert merged.hashtag_count == base.hashtag_count


def test_system_prompt_reflects_settings():
    s = PostSettings(tone="punchy", hashtag_count=3, brand_handle="@x",
                     caption_max_chars=120)
    prompt = _system_for(s)
    assert "punchy" in prompt
    assert "exactly 3" in prompt
    assert "120 characters" in prompt or "120" in prompt
    assert "@x" in prompt


def _sample_post():
    from connect.domain.models import SocialPost
    return SocialPost(headline="A clear and grounded headline about rates",
                      caption="c", hashtags=[],
                      key_points=["point one", "point two"],
                      source_label="Source: RBI", alt_text="a")


def test_card_honours_colors():
    card = render_card(_sample_post(),
                       settings=PostSettings(card_accent="#ff0000",
                                             card_bg="#ffffff"))
    img = Image.open(io.BytesIO(card))
    assert img.format == "JPEG" and img.size == (1080, 1080)
    assert img.getpixel((20, 5))[0] > 180          # accent bar is red
    assert img.getpixel((540, 540))[0] > 240       # white background


def test_all_templates_render():
    post = _sample_post()
    for tmpl in ("classic", "bold", "minimal"):
        for align in ("left", "center"):
            for size in ("s", "m", "l"):
                card = render_card(post, settings=PostSettings(
                    card_template=tmpl, headline_align=align,
                    headline_size=size))
                img = Image.open(io.BytesIO(card))
                assert img.format == "JPEG" and img.size == (1080, 1080)


def test_logo_changes_the_card():
    post = _sample_post()
    base = PostSettings()
    plain = render_card(post, settings=base)
    buf = io.BytesIO()
    Image.new("RGBA", (200, 80), (255, 80, 80, 255)).save(buf, "PNG")
    with_logo = render_card(post, settings=base, logo=buf.getvalue())
    assert plain != with_logo            # the logo is composited in


# --- auto-theme (palette resolution) ----------------------------------------

def test_palette_resolution_precedence():
    from connect.social.palettes import PALETTES, resolve_post_theme

    base = PostSettings()
    # auto-theme off -> identity
    assert resolve_post_theme(base, topic="elections", suggested="gold") == (
        base, None)
    on = PostSettings(auto_theme=True)
    # a topic's default palette wins over the AI suggestion
    _, n = resolve_post_theme(on, topic="elections", suggested="gold")
    assert n == "crimson"
    # a user topic override beats the default
    _, n = resolve_post_theme(
        PostSettings(auto_theme=True, topic_palettes={"elections": "ink"}),
        topic="elections")
    assert n == "ink"
    # unmapped topic -> the AI suggestion is used
    _, n = resolve_post_theme(on, topic="nope", suggested="royal")
    assert n == "royal"
    # a palette swaps colors+template but keeps logo + headline prefs
    themed, n = resolve_post_theme(
        PostSettings(auto_theme=True, logo_sha="a" * 64,
                     headline_align="center"), topic="banking")
    assert themed.logo_sha == "a" * 64 and themed.headline_align == "center"
    assert themed.card_bg == PALETTES[n]["card_bg"]


def test_all_palettes_are_valid_overlays():
    from connect.social.palettes import PALETTES

    for name, pal in PALETTES.items():
        s = PostSettings(**{**PostSettings().model_dump(), **pal})
        render_card(_sample_post(), settings=s)   # renders without error


def test_palettes_endpoint(client):
    body = client.get("/api/social/palettes").json()
    assert "midnight" in body["palettes"] and "crimson" in body["palettes"]
    assert "monetary-policy" in body["topics"]
    assert body["topic_defaults"]["elections"] == "crimson"


def test_auto_theme_settings_roundtrip(client):
    r = client.put("/api/social/settings",
                   json={"auto_theme": True,
                         "topic_palettes": {"elections": "ink"}})
    assert r.status_code == 200, r.text
    assert r.json()["auto_theme"] is True
    assert r.json()["topic_palettes"] == {"elections": "ink"}
    # persisted + falls through to workspaces
    assert client.get("/api/social/settings").json()["auto_theme"] is True
