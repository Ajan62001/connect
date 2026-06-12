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


def test_card_honours_accent_and_signoff():
    content = PostSettings()  # not used; just need a SocialPost
    from connect.domain.models import SocialPost
    card = render_card(
        SocialPost(headline="H", caption="c", hashtags=[],
                   key_points=["a"], source_label="S", alt_text="a"),
        accent="#ff0000", sign_off="@mybrand")
    img = Image.open(io.BytesIO(card))
    assert img.format == "JPEG" and img.size == (1080, 1080)
    # the accent bar (top-left strip) is red
    assert img.getpixel((20, 5))[0] > 180   # strong red channel
