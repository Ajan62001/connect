"""Voices — the reel-narration voice list, the local voicebox preset browser,
and a one-shot audition endpoint (v27).

``GET /voices`` is the canonical voice list (ElevenLabs + 'vb:' voicebox
profiles) — the same payload the deprecated ``/content/voices`` returns. The
``/voices/profiles`` + ``/voices/presets/{engine}`` routes proxy the local
voicebox server so the UI can browse the Kokoro catalog (incl. Hindi voices),
add/remove profiles, and audition any voice.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from connect.api.deps import get_container, get_current_user, require_admin
from connect.content.schema import VoiceOption
from connect.domain.models import CurrentUser
from connect.orchestration.container import Container
from connect.social import tts

router = APIRouter(prefix="/voices", tags=["voices"])

_ALLOWED_ENGINES = {"kokoro", "qwen", "chatterbox", "luxtts", "tada"}


def _require_voicebox(container: Container) -> None:
    if not tts.voicebox_available(container.settings):
        raise HTTPException(status_code=503,
                            detail="voicebox is not configured on this server")


@router.get("", response_model=list[VoiceOption])
async def list_voices(container: Container = Depends(get_container),
                      user: CurrentUser = Depends(get_current_user)):
    return tts.list_voices(container.settings)


@router.get("/profiles")
async def list_profiles(container: Container = Depends(get_container),
                        user: CurrentUser = Depends(get_current_user)):
    """The voicebox server's saved voice profiles (the 'vb:' voices)."""
    return tts.voicebox_profiles(container.settings)


@router.get("/presets/{engine}")
async def list_presets(engine: str,
                       container: Container = Depends(get_container),
                       user: CurrentUser = Depends(get_current_user)):
    """The voicebox preset catalog for an engine (e.g. Kokoro's built-in
    voices, including Hindi ones) — the browse-and-add source."""
    if engine not in _ALLOWED_ENGINES:
        raise HTTPException(status_code=422, detail=f"unknown engine {engine!r}")
    _require_voicebox(container)
    return tts.voicebox_presets(container.settings, engine)


class VoiceboxProfileCreate(BaseModel):
    model_config = ConfigDict(extra="allow")   # pass-through to voicebox


@router.post("/profiles", dependencies=[Depends(require_admin)])
async def create_profile(body: VoiceboxProfileCreate,
                         container: Container = Depends(get_container)):
    """Add a voicebox profile (e.g. from a Kokoro preset). Admin — it mutates
    the shared voicebox server."""
    _require_voicebox(container)
    try:
        return await asyncio.to_thread(
            tts.create_voicebox_profile, container.settings,
            body.model_dump())
    except Exception as e:  # noqa: BLE001 — surface the upstream failure
        raise HTTPException(status_code=502,
                            detail=f"voicebox rejected the profile: {e}")


@router.delete("/profiles/{profile_id}", status_code=204,
               response_class=Response,
               dependencies=[Depends(require_admin)])
async def delete_profile(profile_id: str,
                         container: Container = Depends(get_container)):
    _require_voicebox(container)
    try:
        await asyncio.to_thread(tts.delete_voicebox_profile,
                                container.settings, profile_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502,
                            detail=f"voicebox delete failed: {e}")
    return Response(status_code=204)


class AuditionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: str
    text: str = Field(default="This is a sample of my voice.", max_length=200)


@router.post("/audition")
async def audition(body: AuditionRequest,
                   container: Container = Depends(get_container),
                   user: CurrentUser = Depends(get_current_user)):
    """Synthesize a short clip of ``text`` in ``voice_id`` and stream it back
    (audio/wav for voicebox, audio/mpeg for ElevenLabs)."""
    result = await asyncio.to_thread(
        tts.synthesize_sample, container.settings, body.voice_id, body.text)
    if result is None:
        raise HTTPException(status_code=503,
                            detail="could not synthesize audio for this voice")
    audio, mime = result
    return Response(content=audio, media_type=mime)
