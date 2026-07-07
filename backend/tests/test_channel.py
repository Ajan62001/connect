"""Channel-console tests (v27): characters, script presets, workspace channel
bindings, global channel settings, and the voice/character/preset precedence
resolver."""

from __future__ import annotations


# -- characters ----------------------------------------------------------------

def test_character_crud(client):
    # create
    r = client.post("/api/characters", json={
        "name": "Desi Anchor", "description": "sardonic Delhi news anchor",
        "speaking_style": "fast, punchy, a little cheeky",
        "voice_id": "vb:omega", "sign_off": "Stay sharp.",
        "catchphrases": ["Let's break it down", "Here's the twist"]})
    assert r.status_code == 201, r.text
    ch = r.json()
    cid = ch["id"]
    assert ch["name"] == "Desi Anchor"
    assert ch["voice_id"] == "vb:omega"
    assert ch["catchphrases"] == ["Let's break it down", "Here's the twist"]

    # list + get
    assert any(c["id"] == cid for c in client.get("/api/characters").json())
    assert client.get(f"/api/characters/{cid}").json()["name"] == "Desi Anchor"

    # patch
    r = client.patch(f"/api/characters/{cid}",
                     json={"description": "warmer, still cheeky"})
    assert r.status_code == 200
    assert r.json()["description"] == "warmer, still cheeky"
    assert r.json()["voice_id"] == "vb:omega"   # unchanged

    # delete
    assert client.delete(f"/api/characters/{cid}").status_code == 204
    assert client.get(f"/api/characters/{cid}").status_code == 404


def test_character_length_caps(client):
    # name over 80 chars -> 422
    assert client.post("/api/characters",
                       json={"name": "x" * 81}).status_code == 422
    # >8 catchphrases -> 422
    assert client.post("/api/characters", json={
        "name": "ok", "catchphrases": [str(i) for i in range(9)]
    }).status_code == 422


# -- channel settings (global) -------------------------------------------------

def test_channel_settings_global(client):
    # default is all-null
    r = client.get("/api/social/channel-settings")
    assert r.status_code == 200
    assert r.json() == {"default_voice_id": None,
                        "default_character_id": None,
                        "default_script_type": None}
    # admin update, then read back
    r = client.put("/api/social/channel-settings",
                   json={"default_voice_id": "vb:omega",
                         "default_script_type": "breaking_brief"})
    assert r.status_code == 200
    assert r.json()["default_voice_id"] == "vb:omega"
    got = client.get("/api/social/channel-settings").json()
    assert got["default_voice_id"] == "vb:omega"
    assert got["default_script_type"] == "breaking_brief"


# -- workspace channel binding -------------------------------------------------

def test_workspace_channel_binding(client):
    ws = client.post("/api/workspaces", json={"name": "IndiaShorts"}).json()
    wid = ws["id"]
    # empty (default) channel before any PUT
    r = client.get(f"/api/workspaces/{wid}/channel")
    assert r.status_code == 200
    assert r.json()["platform"] == "youtube"
    assert r.json()["webhook_set"] is False

    ch = client.post("/api/characters", json={"name": "Host"}).json()
    r = client.put(f"/api/workspaces/{wid}/channel", json={
        "platform": "youtube", "channel_name": "India Shorts",
        "channel_handle": "@indiashorts",
        "zapier_webhook_url": "https://hooks.zapier.com/x",
        "default_voice_id": "vb:omega",
        "default_character_id": ch["id"],
        "default_script_type": "explainer"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["channel_name"] == "India Shorts"
    assert got["default_character_id"] == ch["id"]
    # owner sees the webhook + the flag
    reread = client.get(f"/api/workspaces/{wid}/channel").json()
    assert reread["webhook_set"] is True
    assert reread["zapier_webhook_url"] == "https://hooks.zapier.com/x"

    # deleting the bound character unbinds it (ON DELETE SET NULL)
    assert client.delete(f"/api/characters/{ch['id']}").status_code == 204
    assert client.get(
        f"/api/workspaces/{wid}/channel").json()["default_character_id"] is None


# -- script presets ------------------------------------------------------------

def test_script_presets_builtin_and_custom(client):
    presets = client.get("/api/script-presets").json()
    slugs = {p["id"] for p in presets}
    assert {"breaking_brief", "explainer", "listicle", "story_style",
            "fact_check"} <= slugs
    assert all(p["builtin"] for p in presets if p["id"] == "explainer")

    # a builtin cannot be edited
    assert client.patch("/api/script-presets/explainer",
                        json={"name": "x"}).status_code == 403

    # create a custom preset
    r = client.post("/api/script-presets", json={
        "name": "Hot take", "guidance": "spicy opinion, one bold claim",
        "scene_count": 3, "visual_style": "poster"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert pid.startswith("custom:")
    assert r.json()["builtin"] is False

    # it shows up in the list, is patchable + deletable
    assert any(p["id"] == pid for p in client.get("/api/script-presets").json())
    assert client.patch(f"/api/script-presets/{pid}",
                        json={"name": "Hotter take"}).json()["name"] \
        == "Hotter take"
    assert client.delete(f"/api/script-presets/{pid}").status_code == 204


# -- the precedence resolver ---------------------------------------------------

async def test_channel_context_precedence(db):
    from connect.content.channel import resolve_channel_context
    from connect.content.schema import ContentOptions
    from connect.domain.models import ChannelSettings
    from connect.social import channel_settings as cs
    from connect.storage import characters as character_dao
    from connect.storage import users as user_dao
    from connect.storage import workspace_channels as channel_dao
    from connect.storage import workspaces as workspace_dao

    uid = (await user_dao.insert(db, email="owner@test.local",
                                 role="admin"))["id"]
    # a global default character (voice 'vb:global') + a channel character
    gchar = await character_dao.insert(db, owner_id=uid, data={
        "name": "Global", "voice_id": "vb:global"})
    cchar = await character_dao.insert(db, owner_id=uid, data={
        "name": "Channel", "voice_id": "vb:channel"})
    await cs.set_global(db, ChannelSettings(default_character_id=gchar.id,
                                            default_voice_id="vb:globalvoice"))
    ws = await workspace_dao.insert(db, owner_id=uid, name="W", topics=[],
                                    source_ids=[], query_fts=None,
                                    visibility="shared")
    await channel_dao.upsert(db, ws.id, patch={
        "default_character_id": cchar.id, "default_script_type": "explainer"})

    # no per-campaign override => channel character wins over the global one,
    # and the voice folds from the channel character
    ctx = await resolve_channel_context(db, workspace=ws,
                                        options=ContentOptions())
    assert ctx.character.id == cchar.id
    assert ctx.options.voice_id == "vb:channel"        # character's own voice
    assert ctx.preset is not None and ctx.preset.id == "explainer"
    assert ctx.options.script_type == "explainer"
    # the explainer preset's scene_count (4) folds over the default 3
    assert ctx.options.scene_count == 4

    # a per-campaign voice + character override wins over everything
    ctx = await resolve_channel_context(
        db, workspace=ws,
        options=ContentOptions(voice_id="vb:override",
                               character_id=gchar.id))
    assert ctx.character.id == gchar.id
    assert ctx.options.voice_id == "vb:override"

    # no workspace => falls back to the global channel settings' character,
    # and that character's OWN voice wins over the settings default voice
    ctx = await resolve_channel_context(db, workspace=None,
                                        options=ContentOptions())
    assert ctx.character.id == gchar.id
    assert ctx.options.voice_id == "vb:global"     # gchar's own voice


def test_list_voices_is_idempotent():
    """list_voices must not mutate the cached ElevenLabs list — calling it
    repeatedly returns the SAME set (regression: it appended the voicebox
    profiles to the shared cache, duplicating them on every call)."""
    from types import SimpleNamespace
    from unittest import mock
    from connect.social import tts

    tts._voices_cache = None
    settings = SimpleNamespace(elevenlabs_api_key=None, voicebox_url="http://x")
    with mock.patch.object(tts, "voicebox_profiles", return_value=[
            {"id": "p1", "name": "One", "language": "hi"},
            {"id": "p2", "name": "Two", "language": "en"}]):
        first = tts.list_voices(settings)
        second = tts.list_voices(settings)
    assert len(first) == len(second) == 2
    assert {v["id"] for v in second} == {"vb:p1", "vb:p2"}


def test_persona_lines_grounding_safe():
    from connect.content.channel import persona_lines
    from connect.domain.models import Character

    line = persona_lines(Character(
        id=1, name="Anchor", description="sharp",
        speaking_style="punchy", sign_off="Ciao",
        catchphrases=["boom"], created_at="2026-07-03T00:00:00Z"))
    assert "Anchor" in line and "punchy" in line
    # the citation discipline is restated so the persona never outranks it
    assert "[[E#]]" in line
    assert persona_lines(None) == ""
