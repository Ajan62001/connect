"""HeyGen avatar video generation (optional — the "AI presenter" for reels).

Turns a line of script into a photoreal talking-head MP4 via HeyGen's REST API:
generate (``POST /v2/video/generate``) -> poll (``GET /v1/video_status.get``)
-> download the finished MP4 bytes. The classic v2 endpoints are stable and
documented through 2026-10-31; we target them deliberately.

Like the TTS engines this is best-effort and OPTIONAL: nothing here is needed
for the rest of the pipeline, and the reel renderer falls back to its local
slideshow when HeyGen isn't configured or a render fails. Calls run inside the
reel worker thread, so the generate/poll loop is plain blocking ``httpx``.

Costs real HeyGen credits per render — gate every call on :func:`is_configured`.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

API = "https://api.heygen.com"
_GENERATE = f"{API}/v2/video/generate"
_STATUS = f"{API}/v1/video_status.get"
_AVATARS = f"{API}/v2/avatars"
_VOICES = f"{API}/v2/voices"
_UPLOAD = "https://upload.heygen.com/v1/asset"


class HeyGenError(Exception):
    """HeyGen API / render failure — surfaced to the caller (reel falls back)."""


def is_configured(settings) -> bool:
    """True iff an API key AND an avatar id are set (a voice id is also needed
    for HeyGen's own TTS, but we default that if unset)."""
    return bool(getattr(settings, "heygen_api_key", None)
                and getattr(settings, "heygen_avatar_id", None))


def _headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key, "Content-Type": "application/json"}


def _api_error(exc: httpx.HTTPStatusError) -> str:
    try:
        body = exc.response.json()
        err = body.get("error") or body.get("message") or body
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except ValueError:
        msg = exc.response.text[:300]
    return f"HeyGen API error ({exc.response.status_code}): {msg}"


# -- discovery (best-effort; for the experiment script + voices/avatars UI) -----

_avatars_cache: list[dict] | None = None
_voices_cache: list[dict] | None = None


def list_avatars(settings) -> list[dict]:
    """The account's avatars as [{id, name, gender, preview}] (cached). Empty on
    no-key / error."""
    global _avatars_cache
    key = getattr(settings, "heygen_api_key", None)
    if not key:
        return []
    if _avatars_cache is not None:
        return _avatars_cache
    try:
        r = httpx.get(_AVATARS, headers=_headers(key), timeout=30.0)
        r.raise_for_status()
        data = r.json().get("data", {}) or {}
        out = []
        for a in data.get("avatars", []) or []:
            out.append({"id": a.get("avatar_id"),
                        "name": a.get("avatar_name") or a.get("avatar_id"),
                        "gender": a.get("gender", ""),
                        "preview": a.get("preview_image_url", "")})
        _avatars_cache = out
        return out
    except Exception as e:  # noqa: BLE001 — discovery is best-effort
        log.info("could not list HeyGen avatars: %s", e)
        return []


def list_voices(settings) -> list[dict]:
    """The account's voices as [{id, name, language, gender}] (cached). Empty on
    no-key / error."""
    global _voices_cache
    key = getattr(settings, "heygen_api_key", None)
    if not key:
        return []
    if _voices_cache is not None:
        return _voices_cache
    try:
        r = httpx.get(_VOICES, headers=_headers(key), timeout=30.0)
        r.raise_for_status()
        data = r.json().get("data", {}) or {}
        out = []
        for v in data.get("voices", []) or []:
            out.append({"id": v.get("voice_id"),
                        "name": v.get("name") or v.get("voice_id"),
                        "language": v.get("language", ""),
                        "gender": v.get("gender", "")})
        _voices_cache = out
        return out
    except Exception as e:  # noqa: BLE001 — discovery is best-effort
        log.info("could not list HeyGen voices: %s", e)
        return []


# -- asset upload (use our own ElevenLabs narration as the avatar voice) --------

def upload_audio_asset(data: bytes, *, settings, mime: str = "audio/mpeg") -> str:
    """Upload audio bytes to HeyGen and return its asset id — so the avatar can
    lip-sync to OUR ElevenLabs narration instead of HeyGen's own TTS. Raw-binary
    body with the file's Content-Type (not multipart). Raises HeyGenError."""
    key = getattr(settings, "heygen_api_key", None)
    if not key:
        raise HeyGenError("HeyGen not configured (no api key)")
    if not data:
        raise HeyGenError("HeyGen upload: empty audio")
    try:
        r = httpx.post(_UPLOAD, headers={"X-Api-Key": key, "Content-Type": mime},
                       content=data, timeout=120.0)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HeyGenError(_api_error(e)) from e
    body = r.json()
    d = body.get("data") or {}
    asset = d.get("id") or d.get("asset_id") or d.get("audio_asset_id")
    if not asset:
        raise HeyGenError(f"HeyGen upload returned no asset id: {r.text[:300]}")
    return asset


# -- generation ----------------------------------------------------------------

def _build_payload(text: str | None, *, settings, size: tuple[int, int],
                   audio_asset_id: str | None = None) -> dict:
    """The /v2/video/generate body: one avatar scene on a solid background at
    the given vertical dimension. The avatar speaks either an uploaded audio
    asset (our ElevenLabs voice) or ``text`` via HeyGen's own TTS."""
    w, h = size
    if audio_asset_id:
        voice: dict = {"type": "audio", "audio_asset_id": audio_asset_id}
    else:
        voice = {"type": "text", "input_text": text,
                 "speed": float(getattr(settings, "heygen_speed", 1.0) or 1.0)}
        voice_id = getattr(settings, "heygen_voice_id", None)
        if voice_id:
            voice["voice_id"] = voice_id
    bg = getattr(settings, "heygen_background", "#0B1220") or "#0B1220"
    return {
        "video_inputs": [{
            "character": {
                "type": "avatar",
                "avatar_id": getattr(settings, "heygen_avatar_id", None),
                "avatar_style": getattr(settings, "heygen_avatar_style",
                                        "normal") or "normal",
            },
            "voice": voice,
            "background": {"type": "color", "value": bg},
        }],
        "dimension": {"width": int(w), "height": int(h)},
    }


def _start(client: httpx.Client, key: str, payload: dict) -> str:
    """POST the generate request, return the new video_id."""
    r = client.post(_GENERATE, headers=_headers(key), json=payload)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HeyGenError(_api_error(e)) from e
    data = r.json().get("data") or {}
    vid = data.get("video_id")
    if not vid:
        raise HeyGenError(f"HeyGen returned no video_id: {r.text[:300]}")
    return vid


def _await_url(client: httpx.Client, key: str, video_id: str, *,
               timeout_s: float, interval_s: float) -> str:
    """Poll video_status until completed; return the download URL. Raises on
    failure / timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = client.get(_STATUS, headers=_headers(key),
                       params={"video_id": video_id})
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HeyGenError(_api_error(e)) from e
        data = r.json().get("data") or {}
        status = data.get("status")
        if status == "completed":
            url = data.get("video_url") or data.get("video_url_caption")
            if not url:
                raise HeyGenError("HeyGen completed but gave no video_url")
            return url
        if status == "failed":
            err = data.get("error") or "unknown error"
            raise HeyGenError(f"HeyGen render failed: {err}")
        if time.monotonic() >= deadline:
            raise HeyGenError(
                f"HeyGen render timed out after {timeout_s:.0f}s "
                f"(last status: {status})")
        time.sleep(interval_s)


def generate_avatar_clip(text: str | None = None, *, settings,
                         size: tuple[int, int] = (720, 1280),
                         audio_asset_id: str | None = None) -> bytes:
    """Render a talking-avatar MP4 and return its bytes — the avatar speaks
    either ``text`` (HeyGen TTS) or an uploaded ``audio_asset_id`` (our own
    ElevenLabs narration; see :func:`upload_audio_asset`).

    Generate -> poll -> download, all blocking (call from a worker thread).
    Raises :class:`HeyGenError` on any failure so the reel renderer can fall
    back to its local slideshow. ``size`` is (width, height); HeyGen renders at
    a 9:16-friendly 720x1280 by default (cheaper/faster than 1080x1920, and the
    reel upscales/composites it anyway)."""
    key = getattr(settings, "heygen_api_key", None)
    if not key:
        raise HeyGenError("HeyGen not configured (no api key)")
    if not getattr(settings, "heygen_avatar_id", None):
        raise HeyGenError("HeyGen not configured (no avatar id)")
    spoken = (text or "").strip()
    if not audio_asset_id and not spoken:
        raise HeyGenError("HeyGen: empty script")

    timeout_s = float(getattr(settings, "heygen_timeout_s", 300.0) or 300.0)
    interval_s = float(getattr(settings, "heygen_poll_interval_s", 5.0) or 5.0)
    payload = _build_payload(spoken or None, settings=settings, size=size,
                             audio_asset_id=audio_asset_id)
    with httpx.Client(timeout=60.0) as client:
        video_id = _start(client, key, payload)
        log.info("HeyGen render started: video_id=%s", video_id)
        url = _await_url(client, key, video_id,
                         timeout_s=timeout_s, interval_s=interval_s)
        dl = client.get(url, timeout=120.0)
        try:
            dl.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HeyGenError(_api_error(e)) from e
        if not dl.content:
            raise HeyGenError("HeyGen download was empty")
        return dl.content
