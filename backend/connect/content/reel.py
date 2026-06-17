"""Render an Instagram Reel (vertical 9:16 MP4) from a ReelContent (Pillow +
ffmpeg + optional Piper TTS).

(ReelContent, PostSettings) -> MP4 bytes. Per scene we render a themed 1080x1920
frame with Pillow — reusing the card theming helpers from connect/social/card.py
so a reel matches a workspace's cards/carousels — then drive ffmpeg to turn the
stills into a slideshow video. When a Piper voice is available the scenes are
narrated and the audio is muxed (concat keeps audio/video in sync); otherwise a
silent slideshow with crossfades is produced. ffmpeg is required; TTS is not.

Everything intermediate lives in a TemporaryDirectory — the only persistence is
the returned bytes (the caller stores them in the reel BlobStore).
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from connect.content import imagery, video
from connect.content.schema import ReelContent
from connect.domain.models import PostSettings
from connect.social import heygen, tts
from connect.social.card import (
    _BOLD,
    _REG,
    _Theme,
    _draw_block,
    _font,
    _paste_logo,
    _wrap,
)

log = logging.getLogger(__name__)

VW, VH, VMARGIN = 1080, 1920, 96          # reference vertical canvas
_REF_W = float(VW)                        # font sizes are tuned for this width

# per-scene video duration bounds when narrated (seconds)
_MIN_SCENE, _MAX_SCENE = 2.0, 8.0
_SCENE_PAD = 0.45                         # head+tail padding around narration
_XFADE = 0.45                             # scene-to-scene transition duration


class ReelRenderError(Exception):
    """ffmpeg failed to assemble the reel."""


@dataclass(frozen=True)
class ReelProfile:
    size: tuple[int, int] = (VW, VH)
    fps: int = 30
    preset: str = "veryfast"
    crf: int = 23
    crossfade: bool = True               # xfade transitions between scenes
    kenburns: bool = True                # gentle per-scene zoom (motion)
    use_images: bool = True              # fetch web background photos per scene
    use_video: bool = True               # prefer stock VIDEO b-roll per scene
                                         # (needs a Pexels key; falls back to a
                                         # photo, then a themed colour)
    narrate: bool = True                 # synthesize a voiceover (TTS)
    presenter: bool = True               # ALLOW a HeyGen avatar presenter (the
                                         # actual mode is set by settings; OFF in
                                         # TEST_PROFILE so tests never hit HeyGen)
    seconds_per_scene: float = 3.5       # silent fallback per-scene duration
    max_seconds: float = 60.0            # keep reels snappy (well under IG's cap)


# kenburns is safe here: each scene is rendered as its OWN bounded segment
# (-loop 1 + output -t), so zoompan can't over-produce frames across the clip.
PROD_PROFILE = ReelProfile()
TEST_PROFILE = ReelProfile(size=(270, 480), fps=12, preset="ultrafast", crf=30,
                           crossfade=False, kenburns=False, use_images=False,
                           use_video=False, narrate=False, presenter=False,
                           seconds_per_scene=1.0)

# render_reel() uses this when no explicit profile is passed; tests monkeypatch
# it to TEST_PROFILE so campaign runs render tiny, fast, silent reels.
_DEFAULT_PROFILE = PROD_PROFILE


# -- frame rendering -----------------------------------------------------------

_WHITE = (255, 255, 255)
_SHADOW = (0, 0, 0)

# strip emoji/pictographs the bundled font can't render (they'd show as tofu
# boxes) while KEEPING currency (₹), dashes and quotes the news copy needs.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U00002300-\U000023FF"
    "\U0000200D]+")


def _clean(text: str) -> str:
    return _EMOJI.sub("", text or "").strip()


def _geo(query: str) -> str:
    """Bias a background-image search toward India (this is an India-news
    product) unless it already names a place — avoids generic/foreign stock."""
    q = (query or "").strip()
    if not q:
        return q
    ql = q.lower()
    places = ("india", "indian", "mumbai", "delhi", "rbi", "sebi", "modi",
              "bengaluru", "kolkata", "chennai", "hyderabad")
    return q if any(p in ql for p in places) else f"{q} India"


def _cover(img, w: int, h: int):
    """Scale + center-crop ``img`` to exactly fill w x h (no distortion)."""
    from PIL import Image

    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((max(w, int(iw * scale) + 1), max(h, int(ih * scale) + 1)),
                     Image.LANCZOS)
    iw, ih = img.size
    left, top = (iw - w) // 2, (ih - h) // 2
    return img.crop((left, top, left + w, top + h))


def _scrim_alpha(h: int) -> list[int]:
    """Per-row darkening alpha (0-255): light overall darken (keep the colour)
    plus a strong bottom gradient for the lower-third caption and a subtle top
    darken for the logo/accent bar. Shared by the photo scrim and the video
    scrim overlay so both treatments match."""
    col = []
    for y in range(h):
        ty = y / h
        a = 0.26
        if ty > 0.52:
            a += 0.52 * (ty - 0.52) / 0.48
        if ty < 0.16:
            a += 0.26 * (0.16 - ty) / 0.16
        col.append(int(min(0.90, a) * 255))
    return col


def _scrim(img):
    """Darken a PHOTO so white text reads on it (bakes the scrim into the still)."""
    from PIL import Image

    w, h = img.size
    alpha = Image.new("L", (1, h))
    alpha.putdata(_scrim_alpha(h))
    alpha = alpha.resize((w, h))
    black = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(black, img, alpha)


def _scrim_overlay(size: tuple[int, int]) -> "object":
    """The scrim as a transparent RGBA layer (black with the gradient alpha) —
    composited over a VIDEO clip in ffmpeg, since we can't bake it into a still."""
    from PIL import Image

    w, h = size
    alpha = Image.new("L", (1, h))
    alpha.putdata(_scrim_alpha(h))
    alpha = alpha.resize((w, h))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    black.putalpha(alpha)
    return black


def _shadow_block(draw, lines, font, *, y, line_h, w, fill=_WHITE, off=3,
                  align="center", left=0, inner=None):
    """Draw each line with a drop shadow; centred or left-aligned. New y."""
    inner = w if inner is None else inner
    for line in lines:
        tw = draw.textlength(line, font=font)
        x = left + (inner - tw) / 2 if align == "center" else left
        draw.text((x + off, y + off), line, font=font, fill=_SHADOW)
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y


def _draw_scene_caption(draw, *, style: str, heading: str, bullets: list[str],
                        t: _Theme, w: int, h: int, px, margin: int,
                        inner: int) -> None:
    """Draw the scene's on-screen caption in the chosen static style:
    'lower_third' (default), 'centered' (big mid-frame), or 'boxed' (words on
    accent-colour blocks, TikTok-style). 'karaoke' is handled elsewhere (burned
    word-timed subtitles), so it never reaches here."""
    head = heading.strip()
    if not head:
        return

    if style == "centered":
        cap_px = px(96)
        for _try in range(5):
            cap_font = _font(_BOLD, cap_px)
            lines = _wrap(draw, head.upper(), cap_font, inner)
            if len(lines) <= 4 or cap_px <= px(60):
                break
            cap_px = int(cap_px * 0.9)
        lines = lines[:4]
        line_h = int(cap_px * 1.12)
        y0 = int(h * 0.46) - (len(lines) * line_h) // 2
        draw.rectangle([(w - px(110)) // 2, y0 - px(40),
                        (w + px(110)) // 2, y0 - px(28)], fill=t.accent)
        _shadow_block(draw, lines, cap_font, y=y0, line_h=line_h, w=w)
        return

    if style == "boxed":
        cap_px = px(84)
        cap_font = _font(_BOLD, cap_px)
        lines = _wrap(draw, head.upper(), cap_font, int(inner * 0.92))[:3]
        line_h = int(cap_px * 1.30)
        pad_x, pad_y = px(22), px(10)
        y = int(h * 0.72) - len(lines) * line_h
        for line in lines:
            tw = draw.textlength(line, font=cap_font)
            x = (w - tw) / 2
            draw.rectangle([x - pad_x, y - pad_y, x + tw + pad_x,
                            y + cap_px + pad_y], fill=t.accent)
            draw.text((x, y), line, font=cap_font, fill=_WHITE)
            y += line_h
        return

    # default: lower-third with an accent underline + an optional support line
    cap_px = px(86)
    cap_font = _font(_BOLD, cap_px)
    lines = _wrap(draw, head, cap_font, inner)[:3]
    line_h = int(cap_px * 1.12)
    y = int(h * 0.74) - len(lines) * line_h
    bottom = _shadow_block(draw, lines, cap_font, y=y, line_h=line_h, w=w)
    draw.rectangle([margin, bottom + px(14), margin + px(120),
                    bottom + px(24)], fill=t.accent)
    b = next((x.strip() for x in bullets if x.strip()), "")
    if b:
        sf = _font(_REG, px(40))
        _shadow_block(draw, _wrap(draw, b, sf, inner)[:1], sf,
                      y=bottom + px(40), line_h=int(px(40) * 1.3), w=w,
                      fill=(214, 222, 233))


def _scene_bg(bg_image: bytes | None, t: _Theme,
              size: tuple[int, int]) -> "object":
    """The moving layer: a cover-fit photo under a legibility scrim, or the
    themed solid colour. No text — so the Ken-Burns zoom never shakes type."""
    from PIL import Image

    w, h = size
    if bg_image:
        try:
            photo = Image.open(io.BytesIO(bg_image)).convert("RGB")
            return _scrim(_cover(photo, w, h))
        except Exception:  # noqa: BLE001 — a bad image just falls back to colour
            pass
    return Image.new("RGB", (w, h), t.bg)


def _scene_overlay(*, kind: str, heading: str, bullets: list[str],
                   source_label: str, sign_off: str, index: int, total: int,
                   t: _Theme, logo: bytes | None, size: tuple[int, int],
                   show_caption: bool = True,
                   caption_style: str = "lower_third") -> "object":
    """The PINNED layer: a transparent RGBA frame holding the accent bar, logo,
    headline/caption and footer. Composited (static) over the moving bg, so text
    stays rock-steady. The HOOK reads huge and centred; scene captions are drawn
    in ``caption_style`` (lower_third / centered / boxed). ``show_caption=False``
    drops the scene caption (used when burned-in karaoke captions carry the
    words)."""
    from PIL import Image, ImageDraw

    w, h = size
    k = w / _REF_W
    px = lambda n: max(1, int(n * k))           # noqa: E731 — scale helper
    margin = px(VMARGIN)
    inner = w - 2 * margin

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, px(10)], fill=t.accent)     # top accent bar
    _paste_logo(img, logo, canvas_w=w, margin=margin)

    if kind == "title":
        # HOOK: huge, all-caps, centred, with an accent rule above it. Shrink
        # the font for long hooks so nothing wraps past ~4 lines or truncates.
        text = heading.strip().upper()
        head_px = px(118)
        for _try in range(6):
            head_font = _font(_BOLD, head_px)
            lines = _wrap(draw, text, head_font, inner)
            if len(lines) <= 4 or head_px <= px(72):
                break
            head_px = int(head_px * 0.9)
        lines = lines[:5]
        line_h = int(head_px * 1.12)
        y0 = int(h * 0.42) - (len(lines) * line_h) // 2
        draw.rectangle([(w - px(110)) // 2, y0 - px(46),
                        (w + px(110)) // 2, y0 - px(34)], fill=t.accent)
        _shadow_block(draw, lines, head_font, y=y0, line_h=line_h, w=w)
    elif kind == "closing":
        cf = _font(_BOLD, px(64))
        lines = _wrap(draw, (source_label or "connect").strip(), cf, inner)[:3]
        _shadow_block(draw, lines, cf, y=int(h * 0.44), line_h=int(px(64) * 1.15),
                      w=w, fill=t.accent)
    elif not show_caption:
        pass   # karaoke captions (burned subtitles) carry this scene's words
    else:
        _draw_scene_caption(draw, style=caption_style, heading=heading,
                            bullets=bullets, t=t, w=w, h=h, px=px,
                            margin=margin, inner=inner)

    # footer: sign-off + index/total (shadowed for legibility on any photo)
    foot = _font(_BOLD, px(26))
    draw.text((margin + 2, h - margin + 2), sign_off, font=foot, fill=_SHADOW)
    draw.text((margin, h - margin), sign_off, font=foot, fill=_WHITE)
    tag = f"{index}/{total}"
    tw = draw.textlength(tag, font=foot)
    draw.text((w - margin - tw + 2, h - margin + 2), tag, font=foot, fill=_SHADOW)
    draw.text((w - margin - tw, h - margin), tag, font=foot, fill=t.accent)
    return img


# -- ffmpeg assembly -----------------------------------------------------------

_TRANSITIONS = ["fade", "slideleft", "slideright", "smoothup", "fadeblack"]


def _run(ffmpeg: str, args: list[str], out: Path, what: str) -> None:
    proc = subprocess.run([ffmpeg, "-y", *args, str(out)], capture_output=True)
    if proc.returncode != 0 or not out.exists():
        tail = proc.stderr.decode("utf-8", "replace")[-1200:]
        raise ReelRenderError(f"ffmpeg {what} failed (rc={proc.returncode}): {tail}")


# -- karaoke captions (burned ASS subtitles, word-timed to the narration) ------

def _ass_ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _ass_escape(s: str) -> str:
    return (s or "").replace("\\", "").replace("{", "(").replace("}", ")") \
                    .replace("\n", " ")


def _chunk_words(words, max_words=3, max_chars=22):
    """Group words into short on-screen chunks (a few words at a time)."""
    chunks, cur, n = [], [], 0
    for w in words:
        wl = len(w[0])
        if cur and (len(cur) >= max_words or n + wl + 1 > max_chars):
            chunks.append(cur)
            cur, n = [], 0
        cur.append(w)
        n += wl + 1
    if cur:
        chunks.append(cur)
    return chunks


def _build_ass(scenes, voice_durs, scene_words, size) -> str | None:
    """An ASS subtitle: each scene's narration words, chunked and timed on the
    continuous narration timeline (scene i offset by the cumulative durations),
    big/bold/outlined in the lower third — the karaoke captions."""
    w, h = size
    events = []
    offset = 0.0
    for s, dur, words in zip(scenes, voice_durs, scene_words):
        if s["kind"] == "scene" and words:
            for chunk in _chunk_words(words):
                cs = offset + chunk[0][1]
                ce = offset + chunk[-1][2]
                txt = _ass_escape(" ".join(wd[0] for wd in chunk)).upper()
                events.append((cs, max(ce, cs + 0.2), txt))
        offset += dur
    if not events:
        return None
    fs = max(28, int(96 * w / 1080))
    outline = max(2, int(7 * w / 1080))
    marginv = int(560 * h / 1920)
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour,"
        " BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment,"
        " MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,DejaVu Sans,{fs},&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,1,{outline},2,2,70,70,{marginv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV,"
        " Effect, Text\n")
    rows = [head]
    for cs, ce, txt in events:
        rows.append(f"Dialogue: 0,{_ass_ts(cs)},{_ass_ts(ce)},Cap,,0,0,0,,"
                    f"{{\\fad(60,40)}}{txt}")
    return "\n".join(rows)


def _burn_subs(ffmpeg: str, video: Path, ass: Path, profile: ReelProfile,
               out: Path) -> None:
    _run(ffmpeg, ["-i", str(video), "-vf", f"subtitles={ass}",
                  "-r", str(profile.fps), "-c:v", "libx264",
                  "-preset", profile.preset, "-crf", str(profile.crf),
                  "-pix_fmt", "yuv420p"], out, "captions")


def _render_segment(ffmpeg: str, bg: Path, text: Path, dur: float, index: int,
                    profile: ReelProfile, out: Path) -> None:
    """One scene -> a `dur`-second segment: the ``bg`` still gets a gentle
    Ken-Burns zoom (alternating in/out), then the PINNED ``text`` overlay is
    composited on top — so the type never inherits the zoom's pixel jitter and
    can't shake. The zoom runs off the OUTPUT frame counter `on` over a
    `-loop 1` input bounded by the OUTPUT `-t` (the recipe that doesn't multiply
    frames). The bg is supersampled (2x) so its own zoom stays smooth."""
    w, h = profile.size
    fps = profile.fps
    if profile.kenburns:
        n = max(1, int(round(dur * fps)))
        step = 0.08 / n                          # gentle ~8% zoom over the scene
        if index % 2 == 0:
            z = f"min(1.0+{step:.6f}*on,1.08)"   # zoom in
        else:
            z = f"max(1.08-{step:.6f}*on,1.0)"   # zoom out
        bgf = (f"[0:v]zoompan=z='{z}':d={n}"
               f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
               f"setsar=1[bg]")
    else:
        bgf = f"[0:v]scale={w}:{h},fps={fps},setsar=1[bg]"
    fc = f"{bgf};[bg][1:v]overlay=0:0,format=yuv420p[v]"
    _run(ffmpeg, ["-loop", "1", "-i", str(bg), "-loop", "1", "-i", str(text),
                  "-filter_complex", fc, "-map", "[v]", "-t", f"{dur:.3f}",
                  "-r", str(fps), "-c:v", "libx264",
                  "-preset", profile.preset, "-crf", str(profile.crf),
                  "-pix_fmt", "yuv420p"], out, "segment")


def _render_video_segment(ffmpeg: str, clip: Path, overlay: Path, dur: float,
                          profile: ReelProfile, out: Path) -> None:
    """One scene from a STOCK CLIP -> a `dur`-second segment: the clip is
    cover-cropped to 9:16, looped if shorter than the scene, then the combined
    scrim+text ``overlay`` (a pinned RGBA still) is composited on top. The clip's
    own audio is dropped (-an); narration is muxed later. ``-stream_loop -1``
    repeats short clips; ``-t`` bounds the output so long clips are trimmed."""
    w, h = profile.size
    fps = profile.fps
    fc = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},fps={fps},setsar=1[bg];"
          f"[bg][1:v]overlay=0:0,format=yuv420p[v]")
    _run(ffmpeg, ["-stream_loop", "-1", "-i", str(clip),
                  "-loop", "1", "-i", str(overlay),
                  "-filter_complex", fc, "-map", "[v]", "-an",
                  "-t", f"{dur:.3f}", "-r", str(fps), "-c:v", "libx264",
                  "-preset", profile.preset, "-crf", str(profile.crf),
                  "-pix_fmt", "yuv420p"], out, "video-segment")


def _combine(ffmpeg: str, segs: list[Path], durations: list[float],
             profile: ReelProfile, work: Path, out: Path) -> float:
    """Join scene segments into one silent video — xfade transitions when the
    profile asks (and there is >1 scene), else hard-cut concat. Returns the
    combined video duration (xfade overlaps shorten it)."""
    n = len(segs)
    if n == 1:
        _run(ffmpeg, ["-i", str(segs[0]), "-c", "copy"], out, "combine")
        return durations[0]
    if not profile.crossfade:
        listing = work / "segs.txt"
        listing.write_text("".join(f"file '{s}'\n" for s in segs))
        _run(ffmpeg, ["-f", "concat", "-safe", "0", "-i", str(listing),
                      "-c", "copy"], out, "concat")
        return sum(durations)
    # xfade chain — offsets are cumulative, each transition overlaps by _XFADE
    inputs: list[str] = []
    for s in segs:
        inputs += ["-i", str(s)]
    chain, prev, acc = [], "0:v", durations[0]
    for kk in range(1, n):
        label = "v" if kk == n - 1 else f"x{kk}"
        trans = _TRANSITIONS[(kk - 1) % len(_TRANSITIONS)]
        offset = max(0.0, acc - _XFADE)
        chain.append(f"[{prev}][{kk}:v]xfade=transition={trans}"
                     f":duration={_XFADE}:offset={offset:.3f}[{label}]")
        prev, acc = label, acc + durations[kk] - _XFADE
    _run(ffmpeg, [*inputs, "-filter_complex", ";".join(chain), "-map", "[v]",
                  "-r", str(profile.fps), "-c:v", "libx264",
                  "-preset", profile.preset, "-crf", str(profile.crf),
                  "-pix_fmt", "yuv420p"], out, "xfade")
    return acc


def _pick_music(settings, seed: str) -> Path | None:
    """A deterministic music track from the configured dir, or None."""
    d = getattr(settings, "reel_music_dir", None)
    if not d:
        return None
    p = Path(d)
    if not p.is_dir():
        return None
    tracks = sorted(f for f in p.iterdir()
                    if f.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg"))
    if not tracks:
        return None
    return tracks[sum(map(ord, seed)) % len(tracks)]


def _mux(ffmpeg: str, video: Path, narration: Path | None,
         music: Path | None, total: float, settings, out: Path) -> None:
    """Mux the silent video with narration and/or a ducked, looped music bed."""
    if narration is None and music is None:
        _run(ffmpeg, ["-i", str(video), "-c", "copy"], out, "mux")
        return
    mvol = getattr(settings, "reel_music_volume", 0.16) if narration else 0.55
    args = ["-i", str(video)]
    labels = []
    filt = []
    if narration is not None:
        args += ["-i", str(narration)]
        ni = 1
        filt.append(f"[{ni}:a]volume=1.0,apad,atrim=0:{total:.3f}[n]")
        labels.append("[n]")
    if music is not None:
        mi = 2 if narration is not None else 1
        args += ["-stream_loop", "-1", "-i", str(music)]
        fo = max(0.1, total - 1.4)
        filt.append(f"[{mi}:a]volume={mvol},afade=t=in:d=1.2,"
                    f"afade=t=out:st={fo:.3f}:d=1.2,atrim=0:{total:.3f}[m]")
        labels.append("[m]")
    if len(labels) == 2:
        filt.append(f"{labels[0]}{labels[1]}amix=inputs=2:normalize=0[a]")
        amap = "[a]"
    else:
        amap = labels[0]
    _run(ffmpeg, [*args, "-filter_complex", ";".join(filt),
                  "-map", "0:v", "-map", amap, "-c:v", "copy",
                  "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}",
                  "-movflags", "+faststart"], out, "mux")


# -- AI presenter (HeyGen avatar) ----------------------------------------------
# When presenter mode is on AND HeyGen is configured, the narration is delivered
# by a photoreal talking-head avatar instead of (full) / on top of (pip) the
# local slideshow. The avatar speaks with HeyGen's own voice; we composite our
# branding, build the b-roll, and reuse `_mux` for the music bed.


def _presenter_mode(profile: ReelProfile, settings) -> str:
    """The effective presenter mode: 'off', 'pip' or 'full'. Off unless the
    profile allows it, the settings ask for it, AND HeyGen is configured (so
    tests and unconfigured deployments always get the local slideshow)."""
    if not getattr(profile, "presenter", False):
        return "off"
    mode = (getattr(settings, "reel_presenter", "off") or "off").lower()
    if mode not in ("pip", "full"):
        return "off"
    if not heygen.is_configured(settings):
        return "off"
    return mode


def _probe_duration(ffmpeg: str, path: Path) -> float:
    """Media duration in seconds via ffprobe, or 0.0 if it can't be read."""
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe") if "ffmpeg" in ffmpeg \
        else "ffprobe"
    try:
        p = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            capture_output=True)
        return float(p.stdout.decode("utf-8", "replace").strip() or 0.0)
    except Exception:  # noqa: BLE001 — caller treats 0 as a failure
        return 0.0


def _presenter_overlay(*, t: _Theme, logo: bytes | None,
                       size: tuple[int, int], caption: str = "") -> "object":
    """Branding chrome for presenter reels: top accent bar, logo, a footer
    sign-off, and an optional lower-left caption (kept clear of a bottom-right
    avatar in pip mode). Returns a transparent RGBA frame."""
    from PIL import Image, ImageDraw

    w, h = size
    k = w / _REF_W
    px = lambda n: max(1, int(n * k))           # noqa: E731 — scale helper
    margin = px(VMARGIN)
    inner = w - 2 * margin

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, px(10)], fill=t.accent)
    _paste_logo(img, logo, canvas_w=w, margin=margin)

    cap = _clean(caption)
    if cap:
        cap_px = px(72)
        cap_font = _font(_BOLD, cap_px)
        lines = _wrap(draw, cap, cap_font, int(inner * 0.62))[:3]
        line_h = int(cap_px * 1.14)
        y = int(h * 0.70) - len(lines) * line_h
        bottom = _shadow_block(draw, lines, cap_font, y=y, line_h=line_h, w=w,
                               align="left", left=margin, inner=inner)
        draw.rectangle([margin, bottom + px(14), margin + px(120),
                        bottom + px(24)], fill=t.accent)

    foot = _font(_BOLD, px(26))
    draw.text((margin + 2, h - margin + 2), t.sign_off, font=foot, fill=_SHADOW)
    draw.text((margin, h - margin), t.sign_off, font=foot, fill=_WHITE)
    return img


def _presenter_full(ffmpeg: str, avatar: Path, branding: Path, dur: float,
                    profile: ReelProfile, out: Path) -> None:
    """Avatar fills the 9:16 frame (cover-fit) with branding composited on top.
    Output is SILENT (audio is muxed separately)."""
    w, h = profile.size
    fc = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},fps={profile.fps},setsar=1[bg];"
          f"[bg][1:v]overlay=0:0,format=yuv420p[v]")
    _run(ffmpeg, ["-i", str(avatar), "-loop", "1", "-i", str(branding),
                  "-filter_complex", fc, "-map", "[v]", "-an",
                  "-t", f"{dur:.3f}", "-r", str(profile.fps), "-c:v", "libx264",
                  "-preset", profile.preset, "-crf", str(profile.crf),
                  "-pix_fmt", "yuv420p"], out, "presenter-full")


def _presenter_broll(scenes: list[dict], dur: float, *, settings, t: _Theme,
                     logo: bytes | None, profile: ReelProfile, work: Path,
                     ffmpeg: str) -> Path:
    """A silent b-roll slideshow of EXACTLY ``dur`` seconds (hard cuts so the
    total is exact), behind the pip avatar — per-scene web photos under a scrim
    with branding chrome, no big captions (the avatar carries the words)."""
    n = max(1, len(scenes))
    weights = [max(1, len(_clean(s["narration"]))) for s in scenes]
    tot = sum(weights)
    durs = [max(_MIN_SCENE, dur * wi / tot) for wi in weights]
    durs[-1] += dur - sum(durs)              # absorb rounding into the last scene
    durs = [max(0.5, d) for d in durs]

    use_images = (profile.use_images
                  and getattr(settings, "reel_use_images", True))
    w, h = profile.size
    bg_size = (w * 2, h * 2) if profile.kenburns else profile.size
    imgs: list[bytes | None] = [None] * n
    if use_images:
        imgs = [imagery.fetch_scene_image(s["image_query"], settings=settings,
                                          variant=i)
                for i, s in enumerate(scenes)]
        first = next((b for b in imgs if b), None)
        imgs = [b or first for b in imgs]

    branding = _presenter_overlay(t=t, logo=logo, size=profile.size)
    txp = work / "broll_brand.png"
    branding.save(txp, format="PNG")
    segs: list[Path] = []
    for i, s in enumerate(scenes):
        bg_img = _scene_bg(imgs[i], t, bg_size)
        bgp = work / f"broll_bg_{i:02d}.png"
        bg_img.save(bgp, format="PNG")
        seg = work / f"broll_{i:02d}.mp4"
        _render_segment(ffmpeg, bgp, txp, durs[i], i, profile, seg)
        segs.append(seg)
    # hard-cut concat keeps the total EXACTLY `dur` (xfade would shorten it)
    hardcut = ReelProfile(size=profile.size, fps=profile.fps,
                          preset=profile.preset, crf=profile.crf,
                          crossfade=False, kenburns=profile.kenburns)
    out = work / "broll.mp4"
    _combine(ffmpeg, segs, durs, hardcut, work, out)
    return out


def _presenter_pip(ffmpeg: str, broll: Path, avatar: Path, dur: float,
                   profile: ReelProfile, out: Path) -> None:
    """Avatar scaled into a bottom-right card over the b-roll. Output SILENT."""
    w, h = profile.size
    pip_w = int(w * 0.42)
    m = int(w * 0.05)
    fc = (f"[1:v]scale={pip_w}:-2[pip];"
          f"[0:v][pip]overlay=W-w-{m}:H-h-{m}:shortest=0,"
          f"format=yuv420p[v]")
    _run(ffmpeg, ["-i", str(broll), "-i", str(avatar),
                  "-filter_complex", fc, "-map", "[v]", "-an",
                  "-t", f"{dur:.3f}", "-r", str(profile.fps), "-c:v", "libx264",
                  "-preset", profile.preset, "-crf", str(profile.crf),
                  "-pix_fmt", "yuv420p"], out, "presenter-pip")


def _presenter_voice_asset(spoken: str, *, settings, work: Path,
                           ffmpeg: str) -> str | None:
    """When the avatar should speak in OUR ElevenLabs brand voice, synthesize the
    script, transcode to mp3, upload it to HeyGen and return the audio asset id
    (so the avatar lip-syncs to it). Returns None to fall back to HeyGen's own
    TTS — when the source isn't 'elevenlabs', ElevenLabs isn't configured, or any
    step fails. Never raises."""
    source = (getattr(settings, "heygen_voice_source", "elevenlabs")
              or "elevenlabs").lower()
    if source != "elevenlabs" or not getattr(settings, "elevenlabs_api_key", None):
        return None
    try:
        narration, _durs, _words = tts.synthesize_scenes(
            [spoken], settings=settings, work_dir=work)
        if not narration:
            return None
        mp3 = work / "voice_el.mp3"
        _run(ffmpeg, ["-i", str(narration), "-codec:a", "libmp3lame",
                      "-b:a", "128k"], mp3, "presenter-el-mp3")
        asset = heygen.upload_audio_asset(mp3.read_bytes(), settings=settings)
        log.info("presenter: using ElevenLabs brand voice (asset=%s)", asset)
        return asset
    except (heygen.HeyGenError, ReelRenderError) as e:
        log.info("presenter: ElevenLabs voice unavailable (%s); HeyGen TTS", e)
        return None
    except Exception as e:  # noqa: BLE001 — voice is best-effort, never fatal
        log.info("presenter: ElevenLabs voice failed (%s); HeyGen TTS", e)
        return None


def _render_presenter(content: ReelContent, *, mode: str, t: _Theme,
                      logo: bytes | None, tts_settings, profile: ReelProfile,
                      ffmpeg: str) -> bytes:
    """Presenter render path: generate a HeyGen avatar clip of the narration,
    composite it (full-frame or pip over b-roll) with branding, and mux the
    avatar's audio + a ducked music bed. Raises (HeyGenError / ReelRenderError)
    so the caller can fall back to the local slideshow."""
    spoken = " ".join(
        _clean(x) for x in
        [content.title, *(sc.narration for sc in content.scenes)]
        if _clean(x))
    if not spoken:
        raise ReelRenderError("presenter: nothing to narrate")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # speak in our ElevenLabs brand voice when configured (else HeyGen TTS)
        asset = _presenter_voice_asset(spoken, settings=tts_settings, work=work,
                                       ffmpeg=ffmpeg)
        clip = heygen.generate_avatar_clip(
            None if asset else spoken, settings=tts_settings,
            size=(720, 1280), audio_asset_id=asset)
        avatar = work / "avatar.mp4"
        avatar.write_bytes(clip)
        dur = _probe_duration(ffmpeg, avatar)
        if dur <= 0:
            raise ReelRenderError("presenter: could not read avatar duration")
        dur = min(dur, profile.max_seconds)

        # the avatar's own speech becomes the narration track for `_mux`
        voice = work / "voice.wav"
        _run(ffmpeg, ["-i", str(avatar), "-vn", "-ac", "1", "-ar", "22050",
                      "-t", f"{dur:.3f}"], voice, "presenter-voice")

        silent = work / "silent.mp4"
        if mode == "full":
            branding = _presenter_overlay(
                t=t, logo=logo, size=profile.size, caption=content.title)
            bpng = work / "brand.png"
            branding.save(bpng, format="PNG")
            _presenter_full(ffmpeg, avatar, bpng, dur, profile, silent)
        else:  # pip
            scenes = [{"narration": _clean(sc.narration),
                       "image_query": _geo((sc.image_query or "").strip())
                       or _geo(content.image_query or "")}
                      for sc in content.scenes] or [
                {"narration": _clean(content.title),
                 "image_query": _geo(content.image_query or "")}]
            broll = _presenter_broll(scenes, dur, settings=tts_settings, t=t,
                                     logo=logo, profile=profile, work=work,
                                     ffmpeg=ffmpeg)
            _presenter_pip(ffmpeg, broll, avatar, dur, profile, silent)

        music = _pick_music(tts_settings, content.title) \
            if getattr(profile, "use_images", True) else None
        out = work / "reel.mp4"
        _mux(ffmpeg, silent, voice, music, dur, tts_settings, out)
        return out.read_bytes()


# -- public entrypoint ---------------------------------------------------------

def render_reel(content: ReelContent, *, settings: PostSettings,
                logo: bytes | None = None, tts_settings=None,
                profile: ReelProfile | None = None) -> bytes:
    """ReelContent -> MP4 bytes. Renders themed vertical frames, optionally
    narrates them with Piper (silent fallback), and muxes to H.264/AAC via
    ffmpeg. Raises ReelRenderError on ffmpeg failure.

    When presenter mode is enabled (settings.reel_presenter + HeyGen configured)
    a photoreal talking-head avatar delivers the news instead of / over the
    slideshow; any presenter failure falls back to the local slideshow."""
    profile = profile or _DEFAULT_PROFILE
    t = _Theme(settings)
    ffmpeg = getattr(tts_settings, "ffmpeg_path", None) or "ffmpeg"

    mode = _presenter_mode(profile, tts_settings)
    if mode != "off":
        try:
            return _render_presenter(content, mode=mode, t=t, logo=logo,
                                     tts_settings=tts_settings, profile=profile,
                                     ffmpeg=ffmpeg)
        except (heygen.HeyGenError, ReelRenderError) as e:
            log.info("presenter render failed (%s); using local slideshow", e)

    # the hook's image query (falls back to the first scene's subject)
    cover_q = _geo((content.image_query or "").strip() or (
        content.scenes[0].image_query if content.scenes else ""))

    # build the ordered scene list: hook -> content scenes -> optional closing
    scenes: list[dict] = [{"kind": "title", "heading": _clean(content.title),
                           "bullets": [], "narration": _clean(content.title),
                           "image_query": cover_q}]
    for sc in content.scenes:
        scenes.append({"kind": "scene",
                       "heading": _clean(sc.on_screen_caption),
                       "bullets": [_clean(b) for b in sc.bullets],
                       "narration": _clean(sc.narration),
                       "image_query": _geo((sc.image_query or "").strip())
                       or cover_q})
    if (content.source_label or "").strip():
        scenes.append({"kind": "closing", "heading": "", "bullets": [],
                       "narration": content.source_label,
                       "image_query": cover_q})

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if profile.narrate:
            narration, voice_durs, scene_words = tts.synthesize_scenes(
                [s["narration"] for s in scenes], settings=tts_settings,
                work_dir=work)
        else:
            narration, voice_durs, scene_words = None, None, None
        if scene_words is None:
            scene_words = [None] * len(scenes)

        # per-scene video durations; keep the narration durations for caption sync
        if voice_durs is not None:
            durations = [min(_MAX_SCENE, max(_MIN_SCENE, d + _SCENE_PAD))
                         for d in voice_durs]
        else:
            durations = [profile.seconds_per_scene] * len(scenes)
            voice_durs = durations[:]

        # clamp total length to the Reels cap by dropping trailing scenes
        kept, acc = [], 0.0
        for s, d, vd, words in zip(scenes, durations, voice_durs, scene_words):
            if acc + d > profile.max_seconds and kept:
                log.info("reel exceeds %ss cap; dropping trailing scenes",
                         profile.max_seconds)
                break
            kept.append((s, d, vd, words))
            acc += d
        scenes = [x[0] for x in kept]
        durations = [x[1] for x in kept]
        voice_durs = [x[2] for x in kept]
        scene_words = [x[3] for x in kept]

        # caption style: 'karaoke' burns word-timed subtitles (only when the TTS
        # gave word timing), else a static style (lower_third / centered / boxed).
        caption_style = (getattr(tts_settings, "reel_caption_style", "karaoke")
                         or "karaoke").lower()
        if caption_style not in ("karaoke", "lower_third", "centered", "boxed"):
            caption_style = "karaoke"
        has_timing = any(s["kind"] == "scene" and w
                         for s, w in zip(scenes, scene_words))
        karaoke = caption_style == "karaoke" and has_timing
        static_style = "lower_third" if caption_style == "karaoke" \
            else caption_style

        from PIL import Image

        use_video = (profile.use_video
                     and getattr(tts_settings, "reel_use_video", True)
                     and video.available(tts_settings))
        use_images = (profile.use_images
                      and getattr(tts_settings, "reel_use_images", True))
        total_n = len(scenes)

        # per-scene STOCK VIDEO clip (real footage). Scenes without a clip fall
        # back to a photo Ken-Burns still, then to a themed colour.
        vids: list[bytes | None] = [None] * len(scenes)
        if use_video:
            vids = [video.fetch_scene_video(s["image_query"],
                                            settings=tts_settings, variant=i)
                    for i, s in enumerate(scenes)]

        # photos only for the scenes that didn't get a clip; reuse the first hit
        # for any misses so no photo-scene falls back to a flat colour.
        imgs: list[bytes | None] = [None] * len(scenes)
        if use_images:
            imgs = [None if vids[i] else
                    imagery.fetch_scene_image(s["image_query"],
                                              settings=tts_settings, variant=i)
                    for i, s in enumerate(scenes)]
            first = next((b for b in imgs if b), None)
            imgs = [b or (None if vids[i] else first) for i, b in enumerate(imgs)]

        # render one motion segment per scene. The text/chrome overlay is a
        # pinned RGBA still composited on top (so type never inherits motion
        # jitter); for video scenes the scrim is composited into that overlay.
        w, h = profile.size
        bg_size = (w * 2, h * 2) if profile.kenburns else profile.size
        segs: list[Path] = []
        for i, s in enumerate(scenes, start=1):
            txt_img = _scene_overlay(
                kind=s["kind"], heading=s["heading"], bullets=s["bullets"],
                source_label=content.source_label, sign_off=t.sign_off,
                index=i, total=total_n, t=t, logo=logo, size=profile.size,
                show_caption=not karaoke, caption_style=static_style)
            seg = work / f"seg_{i:02d}.mp4"
            if vids[i - 1]:                       # stock video b-roll scene
                clip = work / f"clip_{i:02d}.mp4"
                clip.write_bytes(vids[i - 1])
                overlay = Image.alpha_composite(_scrim_overlay(profile.size),
                                                txt_img)
                ovp = work / f"ov_{i:02d}.png"
                overlay.save(ovp, format="PNG")
                _render_video_segment(ffmpeg, clip, ovp, durations[i - 1],
                                      profile, seg)
            else:                                 # photo / themed-colour scene
                bg_img = _scene_bg(imgs[i - 1], t, bg_size)
                bgp = work / f"bg_{i:02d}.png"
                txp = work / f"tx_{i:02d}.png"
                bg_img.save(bgp, format="PNG")
                txt_img.save(txp, format="PNG")
                _render_segment(ffmpeg, bgp, txp, durations[i - 1], i - 1,
                                profile, seg)
            segs.append(seg)

        # 2. join segments (xfade transitions) into one silent video
        silent = work / "silent.mp4"
        total = _combine(ffmpeg, segs, durations, profile, work, silent)

        # 2b. burn word-timed karaoke captions (synced to the narration)
        rendered = silent
        if karaoke:
            ass_text = _build_ass(scenes, voice_durs, scene_words, profile.size)
            if ass_text:
                ass = work / "cap.ass"
                ass.write_text(ass_text, encoding="utf-8")
                captioned = work / "captioned.mp4"
                try:
                    _burn_subs(ffmpeg, silent, ass, profile, captioned)
                    rendered = captioned
                except ReelRenderError as e:   # captions are a nicety, not vital
                    log.info("caption burn failed, shipping without: %s", e)

        # 3. mux narration + a ducked music bed
        music = (_pick_music(tts_settings, content.title)
                 if getattr(profile, "use_images", True) else None)
        out = work / "reel.mp4"
        _mux(ffmpeg, rendered, narration, music, total, tts_settings, out)
        return out.read_bytes()
