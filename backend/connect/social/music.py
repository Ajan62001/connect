"""AI music generation — "sing the news" via ElevenLabs Music (``/v1/music``).

Turns lyrics into a sung song: either a free-text ``prompt`` (the model writes
+ sings) or a structured ``composition_plan`` (we control the exact lyrics per
section — what we use so the vocals sing the actual news). Returns audio bytes.

Reuses the existing ``elevenlabs_api_key`` (the account's key already has Music
access). Like the TTS engines this is OPTIONAL and best-effort: it raises
``MusicError`` on any failure so callers can fall back. Costs ElevenLabs credits
per song — gate on :func:`is_configured`.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

_COMPOSE = "https://api.elevenlabs.io/v1/music"


class MusicError(Exception):
    """ElevenLabs Music generation failure (caller falls back)."""


def is_configured(settings) -> bool:
    """Music reuses the ElevenLabs key (the account has Music access)."""
    return bool(getattr(settings, "elevenlabs_api_key", None))


def _section(name: str, lines: list[str], *, duration_ms: int,
             styles: list[str] | None = None) -> dict:
    """One SongSection of the composition plan (max 200 chars per line)."""
    return {
        "section_name": name[:100],
        "positive_local_styles": styles or [],
        "negative_local_styles": [],
        "duration_ms": max(3000, min(120000, int(duration_ms))),
        "lines": [ln[:200] for ln in lines if ln and ln.strip()],
    }


def build_news_plan(sections: list[tuple[str, list[str]]], *,
                    styles: list[str] | None = None,
                    seconds_per_section: float = 14.0) -> dict:
    """A composition_plan from ``(section_name, lines)`` pairs — an upbeat,
    radio-news-jingle feel by default. ``styles`` overrides the global vibe."""
    glob = styles or ["upbeat news jingle", "catchy pop", "clear vocals",
                       "modern radio production", "energetic"]
    return {
        "positive_global_styles": glob,
        "negative_global_styles": ["lo-fi", "mumbled vocals", "heavy metal",
                                   "explicit"],
        "sections": [_section(name, lines,
                              duration_ms=int(seconds_per_section * 1000))
                     for name, lines in sections if lines],
    }


def compose_song(*, settings, prompt: str | None = None,
                 composition_plan: dict | None = None,
                 length_ms: int | None = None,
                 force_instrumental: bool = False,
                 output_format: str = "mp3_44100_128") -> bytes:
    """Generate a song and return its audio bytes. Provide either a
    ``composition_plan`` (exact lyrics per section — preferred for the news) or a
    free-text ``prompt`` (+ ``length_ms``). Raises :class:`MusicError`."""
    key = getattr(settings, "elevenlabs_api_key", None)
    if not key:
        raise MusicError("ElevenLabs not configured (no api key)")
    if not composition_plan and not (prompt or "").strip():
        raise MusicError("music: need a prompt or a composition_plan")

    body: dict = {"model_id": getattr(settings, "elevenlabs_music_model",
                                      "music_v1"),
                  "force_instrumental": force_instrumental}
    if composition_plan:
        body["composition_plan"] = composition_plan
    else:
        body["prompt"] = prompt
        body["music_length_ms"] = max(3000, min(600000, int(length_ms or 30000)))
    try:
        r = httpx.post(_COMPOSE, params={"output_format": output_format},
                       headers={"xi-api-key": key,
                                "Content-Type": "application/json"},
                       json=body, timeout=240.0)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:300]
        raise MusicError(f"ElevenLabs Music error "
                         f"({e.response.status_code}): {detail}") from e
    except httpx.HTTPError as e:
        raise MusicError(f"ElevenLabs Music request failed: {e}") from e
    if not r.content:
        raise MusicError("ElevenLabs Music returned no audio")
    return r.content
