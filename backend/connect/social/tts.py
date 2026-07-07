"""Optional text-to-speech for reel narration — three engines, all optional.

``synthesize_scenes`` picks an engine (``_resolve_engine``): ElevenLabs cloud
(most natural, needs ``elevenlabs_api_key``), a local voicebox server
(free/offline, profile-based — its Kokoro presets include Hindi/Indian-accented
voices; needs ``voicebox_url``), or local Piper (free, offline, needs a baked
voice). 'auto' prefers ElevenLabs, then voicebox, then Piper; failures fall
through the same order, then to silent. TTS NEVER hard-fails reel generation —
a silent slideshow is always a valid result.

- ElevenLabs: REST API, `xi-api-key`; per-scene mp3 normalised to wav via ffmpeg.
- voicebox: local HTTP API — POST /generate (async) -> poll /history/{id} ->
  GET /audio/{id}; per-scene wav normalised via ffmpeg. No word timing, so
  karaoke captions fall back to static ones.
- Piper: a ``<voice>.onnx`` + ``.onnx.json`` pair under
  ``settings.piper_voice_dir`` (env ``CONNECT_PIPER_VOICE_DIR``); the `piper`
  package is the optional ``[tts]`` extra, imported lazily.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

log = logging.getLogger(__name__)


def _voice_paths(settings) -> tuple[Path, Path] | None:
    """The (onnx, onnx.json) pair if both exist, else None."""
    vdir = getattr(settings, "piper_voice_dir", None)
    if not vdir:
        return None
    voice = getattr(settings, "piper_voice", "en_US-lessac-medium")
    onnx = Path(vdir) / f"{voice}.onnx"
    config = Path(vdir) / f"{voice}.onnx.json"
    if onnx.exists() and config.exists():
        return onnx, config
    return None


def piper_available(settings) -> bool:
    """True iff the Piper package imports AND the voice model files exist."""
    if _voice_paths(settings) is None:
        return False
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return True


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        frames, rate = w.getnframes(), w.getframerate()
    return frames / float(rate) if rate else 0.0


def _syn_config():
    """A slightly slower, more expressive synthesis config — less clipped/robotic
    than the defaults. Returns None if this piper build has no SynthesisConfig."""
    try:
        from piper import SynthesisConfig
    except ImportError:
        return None
    # length_scale>1 slows speech (clearer, less rushed); noise_w adds prosody
    # variation so it sounds less monotone.
    return SynthesisConfig(length_scale=1.06, noise_scale=0.667,
                           noise_w_scale=0.9, normalize_audio=True)


def _synthesize_one(voice, text: str, out_wav: Path, syn) -> None:
    """Write ``text`` to ``out_wav`` via Piper. Tolerates API differences
    across piper-tts versions (synthesize_wav vs synthesize)."""
    with wave.open(str(out_wav), "wb") as wf:
        if hasattr(voice, "synthesize_wav"):
            if syn is not None:
                voice.synthesize_wav(text, wf, syn_config=syn)
            else:
                voice.synthesize_wav(text, wf)
        else:  # older API: synthesize(text, wav_file)
            voice.synthesize(text, wf)


def _concat_wavs(parts: list[Path], out: Path) -> None:
    """Concatenate same-format WAVs into one (Piper output shares params)."""
    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
    with wave.open(str(out), "wb") as combined:
        combined.setparams(params)
        for p in parts:
            with wave.open(str(p), "rb") as w:
                combined.writeframes(w.readframes(w.getnframes()))


_voices_cache: list[dict] | None = None


def list_elevenlabs_voices(settings) -> list[dict]:
    """The account's ElevenLabs voices as [{id, name, description}] (cached in
    process). Empty list when no key / on error."""
    global _voices_cache
    key = getattr(settings, "elevenlabs_api_key", None)
    if not key:
        return []
    if _voices_cache is not None:
        return _voices_cache
    try:
        import httpx
        r = httpx.get("https://api.elevenlabs.io/v1/voices",
                      headers={"xi-api-key": key}, timeout=20.0)
        r.raise_for_status()
        out = []
        for v in r.json().get("voices", []):
            labels = v.get("labels", {}) or {}
            desc = ", ".join(x for x in (labels.get("descriptive"),
                                         labels.get("accent"),
                                         labels.get("gender")) if x)
            out.append({"id": v.get("voice_id"), "name": v.get("name"),
                        "description": desc})
        _voices_cache = out
        return out
    except Exception as e:  # noqa: BLE001 — voices list is best-effort
        log.info("could not list ElevenLabs voices: %s", e)
        return []


# -- voicebox (local TTS server) ------------------------------------------------

VOICEBOX_PREFIX = "vb:"                    # voice-picker ids: 'vb:<profile_id>'


def voicebox_available(settings) -> bool:
    return bool(getattr(settings, "voicebox_url", None))


def _voicebox_base(settings) -> str:
    return str(getattr(settings, "voicebox_url", "") or "").rstrip("/")


def voicebox_profiles(settings) -> list[dict]:
    """The server's voice profiles (uncached — the call is local and instant).
    Empty list when unconfigured / unreachable."""
    if not voicebox_available(settings):
        return []
    try:
        import httpx
        r = httpx.get(f"{_voicebox_base(settings)}/profiles", timeout=10.0)
        r.raise_for_status()
        return r.json() or []
    except Exception as e:  # noqa: BLE001 — voices list is best-effort
        log.info("could not list voicebox profiles: %s", e)
        return []


def voicebox_presets(settings, engine: str) -> list[dict]:
    """The voicebox server's PRESET catalog for an engine (e.g. kokoro's built-in
    voices, including Hindi ones). Empty list when unconfigured / unreachable."""
    if not voicebox_available(settings):
        return []
    try:
        import httpx
        r = httpx.get(f"{_voicebox_base(settings)}/profiles/presets/{engine}",
                      timeout=10.0)
        r.raise_for_status()
        return r.json() or []
    except Exception as e:  # noqa: BLE001 — catalog is best-effort
        log.info("could not list voicebox presets (%s): %s", engine, e)
        return []


def create_voicebox_profile(settings, body: dict) -> dict:
    """Create a voicebox profile (e.g. from a preset). Raises on failure."""
    import httpx
    r = httpx.post(f"{_voicebox_base(settings)}/profiles", json=body,
                   timeout=30.0)
    r.raise_for_status()
    return r.json()


def delete_voicebox_profile(settings, profile_id: str) -> None:
    """Delete a voicebox profile. Raises on failure."""
    import httpx
    r = httpx.delete(f"{_voicebox_base(settings)}/profiles/{profile_id}",
                     timeout=15.0)
    r.raise_for_status()


def list_voices(settings) -> list[dict]:
    """Everything the studio's voice picker can offer: the ElevenLabs account
    voices plus the local voicebox profiles (ids prefixed ``vb:`` — picking one
    switches that render to the voicebox engine)."""
    # copy: list_elevenlabs_voices returns the cached list BY REFERENCE, so we
    # must not append to it (that permanently grows the process cache and yields
    # duplicate voicebox entries on every subsequent call).
    out = list(list_elevenlabs_voices(settings))
    for p in voicebox_profiles(settings):
        bits = [x for x in ((p.get("language") or "").upper() or None,
                            p.get("description")) if x]
        out.append({"id": f"{VOICEBOX_PREFIX}{p['id']}",
                    "name": f"{p.get('name', 'voice')} (voicebox)",
                    "description": " · ".join(bits) or "local voicebox voice"})
    return out


def synthesize_sample(settings, voice_id: str,
                      text: str) -> tuple[bytes, str] | None:
    """Synthesize a short AUDITION clip of ``text`` in ``voice_id`` and return
    (audio_bytes, mime), or None on failure. A 'vb:<profile>' id uses voicebox
    (wav); anything else an ElevenLabs voice (mp3). Blocking — the router runs
    it in a thread. Never raises."""
    text = (text or "").strip() or "This is a sample of my voice."
    try:
        if voice_id.startswith(VOICEBOX_PREFIX):
            return _voicebox_sample(settings, voice_id[len(VOICEBOX_PREFIX):],
                                    text)
        return _elevenlabs_sample(settings, voice_id, text)
    except Exception as e:  # noqa: BLE001 — audition is best-effort
        log.info("voice audition failed (%s): %s", voice_id, e)
        return None


def _voicebox_sample(settings, profile_id: str,
                     text: str) -> tuple[bytes, str] | None:
    import time

    import httpx
    if not voicebox_available(settings):
        return None
    base = _voicebox_base(settings)
    budget = float(getattr(settings, "voicebox_timeout_s", 300.0) or 300.0)
    language = getattr(settings, "voicebox_language", "en") or "en"
    with httpx.Client(timeout=30.0) as c:
        prof = c.get(f"{base}/profiles/{profile_id}")
        prof.raise_for_status()
        body = {"profile_id": profile_id, "language": language, "text": text}
        engine = (prof.json() or {}).get("default_engine")
        if engine:
            body["engine"] = engine
        r = c.post(f"{base}/generate", json=body)
        r.raise_for_status()
        gid = r.json()["id"]
        status = r.json().get("status") or "generating"
        deadline = time.monotonic() + budget
        while status not in ("completed", "failed"):
            if time.monotonic() > deadline:
                return None
            time.sleep(1.0)
            status = (c.get(f"{base}/history/{gid}").json()
                      .get("status") or "generating")
        if status == "failed":
            return None
        audio = c.get(f"{base}/audio/{gid}")
        audio.raise_for_status()
        return audio.content, "audio/wav"


def _elevenlabs_sample(settings, voice_id: str,
                       text: str) -> tuple[bytes, str] | None:
    import httpx
    key = getattr(settings, "elevenlabs_api_key", None)
    if not key:
        return None
    model = getattr(settings, "elevenlabs_model", "eleven_multilingual_v2")
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": text, "model_id": model}, timeout=60.0)
    r.raise_for_status()
    return r.content, "audio/mpeg"


def _resolve_engine(settings) -> str:
    """Which TTS engine to use: explicit ``reel_tts_engine`` setting, or 'auto'
    (ElevenLabs if a key is set, else voicebox if a URL is set, else Piper if
    a voice is present, else none)."""
    eng = (getattr(settings, "reel_tts_engine", "auto") or "auto").lower()
    if eng != "auto":
        return eng
    if getattr(settings, "elevenlabs_api_key", None):
        return "elevenlabs"
    if voicebox_available(settings):
        return "voicebox"
    if piper_available(settings):
        return "piper"
    return "none"


def _piper_parts(scene_texts, settings, work_dir) -> list[Path] | None:
    """One Piper WAV per scene, or None if Piper/voice is unavailable."""
    paths = _voice_paths(settings)
    if paths is None:
        return None
    onnx, config = paths
    try:
        from piper import PiperVoice
        voice = PiperVoice.load(str(onnx), config_path=str(config))
        syn = _syn_config()
        parts = []
        for i, text in enumerate(scene_texts):
            wav = work_dir / f"piper_{i}.wav"
            _synthesize_one(voice, (text or "").strip() or " ", wav, syn)
            parts.append(wav)
        return parts or None
    except Exception as e:  # noqa: BLE001 — best-effort
        log.info("piper TTS failed: %s", e)
        return None


def _voicebox_profile(settings, client) -> dict | None:
    """The profile to speak with: ``voicebox_profile_id`` when set (and still
    present on the server), else the server's first profile."""
    profiles = []
    try:
        r = client.get(f"{_voicebox_base(settings)}/profiles", timeout=10.0)
        r.raise_for_status()
        profiles = r.json() or []
    except Exception as e:  # noqa: BLE001 — treated as no-profile below
        log.info("voicebox profiles unavailable: %s", e)
    if not profiles:
        return None
    want = getattr(settings, "voicebox_profile_id", None)
    if want:
        for p in profiles:
            if p.get("id") == want:
                return p
        log.info("voicebox profile %s not found; using %r", want,
                 profiles[0].get("name"))
    return profiles[0]


def _voicebox_parts(scene_texts, settings, work_dir) -> list[Path] | None:
    """One WAV per scene via the local voicebox server, or None on any failure
    (unconfigured, unreachable, generation error) so the caller can fall back.
    POST /generate is async: poll /history/{id} until completed, then download
    /audio/{id} and normalise to the shared wav format for _concat_wavs."""
    if not voicebox_available(settings):
        return None
    import subprocess
    import time

    import httpx
    base = _voicebox_base(settings)
    budget = float(getattr(settings, "voicebox_timeout_s", 300.0) or 300.0)
    language = getattr(settings, "voicebox_language", "en") or "en"
    ffmpeg = getattr(settings, "ffmpeg_path", None) or "ffmpeg"
    try:
        parts: list[Path] = []
        with httpx.Client(timeout=30.0) as c:
            profile = _voicebox_profile(settings, c)
            if profile is None:
                return None
            body_base = {"profile_id": profile["id"], "language": language}
            # preset profiles only accept their own engine (e.g. kokoro)
            if profile.get("default_engine"):
                body_base["engine"] = profile["default_engine"]
            for i, text in enumerate(scene_texts):
                spoken = (text or "").strip() or " "
                r = c.post(f"{base}/generate",
                           json={**body_base, "text": spoken})
                r.raise_for_status()
                gid = r.json()["id"]
                deadline = time.monotonic() + budget
                status = r.json().get("status") or "generating"
                while status not in ("completed", "failed"):
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"voicebox scene {i} timed out")
                    time.sleep(1.0)
                    status = (c.get(f"{base}/history/{gid}").json()
                              .get("status") or "generating")
                if status == "failed":
                    raise RuntimeError(f"voicebox scene {i} failed")
                audio = c.get(f"{base}/audio/{gid}")
                audio.raise_for_status()
                raw = work_dir / f"vb_{i}.src"
                raw.write_bytes(audio.content)
                wav = work_dir / f"vb_{i}.wav"
                # normalise to a common wav format so _concat_wavs lines up
                p = subprocess.run(
                    [ffmpeg, "-y", "-i", str(raw), "-ar", "22050", "-ac", "1",
                     str(wav)], capture_output=True)
                if p.returncode != 0 or not wav.exists():
                    raise RuntimeError("ffmpeg voicebox->wav failed")
                parts.append(wav)
        return parts or None
    except Exception as e:  # noqa: BLE001 — fall back to piper/silent
        log.info("voicebox TTS failed, falling back: %s", e)
        return None


def _words_from_alignment(al: dict | None) -> list[tuple[str, float, float]] | None:
    """Aggregate ElevenLabs character alignment into (word, start, end) tuples
    (relative to the scene's audio) — the timing for karaoke captions."""
    if not al:
        return None
    chars = al.get("characters") or []
    st = al.get("character_start_times_seconds") or []
    et = al.get("character_end_times_seconds") or []
    words: list[tuple[str, float, float]] = []
    cur, ws, we = "", None, None
    for c, a, b in zip(chars, st, et):
        if c.isspace():
            if cur:
                words.append((cur, ws or 0.0, we or 0.0))
                cur, ws, we = "", None, None
        else:
            if not cur:
                ws = a
            cur += c
            we = b
    if cur:
        words.append((cur, ws or 0.0, we or 0.0))
    return words or None


def _elevenlabs_parts(scene_texts, settings, work_dir):
    """ElevenLabs (natural voice) per scene -> (wavs, words_per_scene) with
    word-level timing for karaoke captions, or None on any failure (no key,
    network, quota) so the caller can fall back."""
    key = getattr(settings, "elevenlabs_api_key", None)
    if not key:
        return None
    import base64
    import subprocess

    import httpx
    voice = getattr(settings, "elevenlabs_voice_id", "JBFqnCBsd6RMkjVDRZzb")
    model = getattr(settings, "elevenlabs_model", "eleven_multilingual_v2")
    ffmpeg = getattr(settings, "ffmpeg_path", None) or "ffmpeg"
    try:
        parts, words_per = [], []
        with httpx.Client(timeout=60.0) as c:
            for i, text in enumerate(scene_texts):
                spoken = (text or "").strip() or " "
                r = c.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
                    f"/with-timestamps",
                    headers={"xi-api-key": key,
                             "Content-Type": "application/json"},
                    json={"text": spoken, "model_id": model,
                          "voice_settings": {"stability": 0.45,
                                             "similarity_boost": 0.8,
                                             "style": 0.0,
                                             "use_speaker_boost": True}})
                r.raise_for_status()
                d = r.json()
                mp3 = work_dir / f"el_{i}.mp3"
                mp3.write_bytes(base64.b64decode(d["audio_base64"]))
                wav = work_dir / f"el_{i}.wav"
                # normalise to a common wav format so _concat_wavs lines up
                p = subprocess.run(
                    [ffmpeg, "-y", "-i", str(mp3), "-ar", "22050", "-ac", "1",
                     str(wav)], capture_output=True)
                if p.returncode != 0 or not wav.exists():
                    raise RuntimeError("ffmpeg mp3->wav failed")
                parts.append(wav)
                words_per.append(_words_from_alignment(d.get("alignment")))
        return (parts, words_per) if parts else None
    except Exception as e:  # noqa: BLE001 — fall back to piper/silent
        log.info("ElevenLabs TTS failed, falling back: %s", e)
        return None


def synthesize_scenes(scene_texts: list[str], *, settings, work_dir: Path):
    """Synthesize one WAV per scene and concatenate them, in order, into a
    single narration WAV. Picks the engine (ElevenLabs cloud or local Piper) per
    ``_resolve_engine``; ElevenLabs falls back to Piper, then to silent.

    Returns ``(narration_wav, per_scene_durations, per_scene_words)`` where
    per_scene_words[i] is a list of ``(word, start, end)`` (relative to that
    scene's audio) for karaoke captions, or None when the engine has no word
    timing (voicebox/Piper). Returns ``(None, None, None)`` when there's no
    narration. Never raises.
    """
    engine = _resolve_engine(settings)
    parts: list[Path] | None = None
    words: list | None = None
    if engine == "elevenlabs":
        res = _elevenlabs_parts(scene_texts, settings, work_dir)
        if res is not None:
            parts, words = res
        if parts is None:                       # graceful fallback to local
            parts = _voicebox_parts(scene_texts, settings, work_dir) \
                or _piper_parts(scene_texts, settings, work_dir)
    elif engine == "voicebox":
        parts = _voicebox_parts(scene_texts, settings, work_dir) \
            or _piper_parts(scene_texts, settings, work_dir)
    elif engine == "piper":
        parts = _piper_parts(scene_texts, settings, work_dir)

    if not parts:
        return None, None, None
    if words is None:
        words = [None] * len(parts)
    durations = [_wav_duration(p) for p in parts]
    narration = work_dir / "narration.wav"
    _concat_wavs(parts, narration)
    return narration, durations, words
