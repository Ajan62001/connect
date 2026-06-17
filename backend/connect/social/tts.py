"""Optional text-to-speech for reel narration — two engines, both optional.

``synthesize_scenes`` picks an engine (``_resolve_engine``): ElevenLabs cloud
(most natural, needs ``elevenlabs_api_key``) or local Piper (free, offline,
needs a baked voice). 'auto' prefers ElevenLabs then Piper; ElevenLabs failures
fall back to Piper, then to silent. TTS NEVER hard-fails reel generation — a
silent slideshow is always a valid result.

- ElevenLabs: REST API, `xi-api-key`; per-scene mp3 normalised to wav via ffmpeg.
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


def _resolve_engine(settings) -> str:
    """Which TTS engine to use: explicit ``reel_tts_engine`` setting, or 'auto'
    (ElevenLabs if a key is set, else Piper if a voice is present, else none)."""
    eng = (getattr(settings, "reel_tts_engine", "auto") or "auto").lower()
    if eng != "auto":
        return eng
    if getattr(settings, "elevenlabs_api_key", None):
        return "elevenlabs"
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
    timing (Piper). Returns ``(None, None, None)`` when there's no narration.
    Never raises.
    """
    engine = _resolve_engine(settings)
    parts: list[Path] | None = None
    words: list | None = None
    if engine == "elevenlabs":
        res = _elevenlabs_parts(scene_texts, settings, work_dir)
        if res is not None:
            parts, words = res
        if parts is None:                       # graceful fallback to local
            parts = _piper_parts(scene_texts, settings, work_dir)
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
