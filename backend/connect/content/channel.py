"""The channel-context resolver — the one place a campaign's effective voice,
character, script-type and card settings are assembled from the precedence chain.

Precedence (least -> most specific):
    env Settings  <  global ChannelSettings  <  workspace_channel  <  character
    <  per-campaign ContentOptions

A workspace binds channel defaults; a character carries its own voice; the
campaign's ContentOptions override anything explicitly set. The resolver folds
those defaults into the *unset* knobs of the ContentOptions (so the existing
render path — which reads ``options.voice_id`` etc. — needs no precedence logic
of its own), and hands back the effective PostSettings (card look) plus the
resolved character + script preset for the prompt builders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from connect.content.presets import ScriptPreset, get_builtin, preset_lines
from connect.content.schema import ContentOptions
from connect.domain.models import Character, PostSettings
from connect.social import channel_settings as channel_settings_mod
from connect.social import settings as post_settings
from connect.storage import characters as character_dao
from connect.storage import script_presets as preset_dao
from connect.storage import workspace_channels as channel_dao


@dataclass
class ChannelContext:
    post: PostSettings                 # effective card/caption settings
    channel: dict[str, Any] | None     # raw workspace_channel row (or None)
    character: Character | None
    preset: ScriptPreset | None
    options: ContentOptions            # defaults folded into unset knobs


def persona_lines(character: Character | None) -> str:
    """The framing-only persona prompt line (voice ONLY — the persona knows no
    facts; every claim still cites [[E#]]). Empty when there is no character."""
    if character is None:
        return ""
    bits = [f"write as {character.name}"]
    if (character.description or "").strip():
        bits.append(f"— {character.description.strip()}")
    line = "PERSONA (voice/tone ONLY): " + " ".join(bits) + "."
    if (character.speaking_style or "").strip():
        line += f" Speaking style: {character.speaking_style.strip()}."
    if (character.sign_off or "").strip():
        line += f" Sign off with: '{character.sign_off.strip()}'."
    phrases = [p for p in (character.catchphrases or []) if p.strip()][:4]
    if phrases:
        line += (" Catchphrases to use sparingly where they fit naturally: "
                 + "; ".join(phrases) + ".")
    line += (" The persona shapes VOICE only — it invents no facts; every"
             " factual claim still cites [[E#]] from the evidence menu.\n")
    return line


async def resolve_preset(conn: psycopg.AsyncConnection,
                         script_type: str | None) -> ScriptPreset | None:
    """A script-type pick (builtin slug or 'custom:<id>') -> ScriptPreset, or
    None when unset / unknown."""
    if not script_type:
        return None
    if script_type.startswith(preset_dao.CUSTOM_PREFIX):
        raw = script_type[len(preset_dao.CUSTOM_PREFIX):]
        try:
            return await preset_dao.get(conn, int(raw))
        except (ValueError, TypeError):
            return None
    return get_builtin(script_type)


async def resolve_channel_context(
        conn: psycopg.AsyncConnection, *, workspace: Any,
        options: ContentOptions) -> ChannelContext:
    """Assemble the effective channel context for a campaign. ``workspace`` is
    the Workspace model (or None for a non-workspace campaign)."""
    post = await post_settings.effective(conn, workspace)
    globals_ = await channel_settings_mod.get_global(conn)
    channel = (await channel_dao.get_raw(conn, workspace.id)
               if workspace is not None else None)

    def chan(key: str) -> Any:
        return channel.get(key) if channel else None

    # character: options > channel default > global default
    char_id = (options.character_id
               or chan("default_character_id")
               or globals_.default_character_id)
    character = await character_dao.get(conn, char_id) if char_id else None

    # script type: options > channel default > global default
    script_type = (options.script_type
                   or chan("default_script_type")
                   or globals_.default_script_type)
    preset = await resolve_preset(conn, script_type)

    # voice: options > character > channel default > global default
    voice_id = (options.voice_id
                or (character.voice_id if character else None)
                or chan("default_voice_id")
                or globals_.default_voice_id)

    upd: dict[str, Any] = {}
    if voice_id and not options.voice_id:
        upd["voice_id"] = voice_id
    if char_id and options.character_id is None:
        upd["character_id"] = char_id
    if script_type and not options.script_type:
        upd["script_type"] = script_type
    # preset render defaults fold into unset knobs (scene_count folds when the
    # caller left it at its schema default — the one ambiguous knob, per plan)
    if preset is not None:
        if preset.scene_count and options.scene_count == 3:
            upd["scene_count"] = preset.scene_count
        if preset.caption_style and options.caption_style is None:
            upd["caption_style"] = preset.caption_style
        if preset.visual_style and options.visual_style is None:
            upd["visual_style"] = preset.visual_style

    resolved = options.model_copy(update=upd) if upd else options
    return ChannelContext(post=post, channel=channel, character=character,
                          preset=preset, options=resolved)


def style_context(ctx: ChannelContext) -> str:
    """The combined persona + preset framing lines for the reel editor loop."""
    return persona_lines(ctx.character) + preset_lines(ctx.preset)
