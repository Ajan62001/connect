"""Render a 1080×1080 Instagram card from a SocialPost (Pillow).

Pure: (SocialPost, PostSettings) -> JPEG bytes, no network. The whole look is
driven by the effective PostSettings — colors (bg/text/muted/accent), one of
three layout templates (classic / bold / minimal), headline size + alignment,
and an optional brand logo composited top-right. Fonts load from common Linux
paths with a graceful fallback to Pillow's bitmap font, so rendering never
hard-fails on a font-less box.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

from connect.domain.models import PostSettings, SocialPost

SIZE = 1080
MARGIN = 84
# legacy default palette (kept as named constants so connect/content/render.py
# and older callers keep importing them; live rendering uses PostSettings).
BG = (15, 23, 42)        # slate-900
ACCENT = (56, 189, 248)  # sky-400
TEXT = (241, 245, 249)   # slate-100
MUTED = (148, 163, 184)  # slate-400

_HEAD_PX = {"s": 62, "m": 78, "l": 96}

_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
_REG = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def _font(paths: Sequence[str], size: int):
    from PIL import ImageFont
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _hex_to_rgb(value: str | None,
                fallback: tuple[int, int, int] = ACCENT) -> tuple[int, int, int]:
    v = (value or "").lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) == 6:
        try:
            return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
        except ValueError:
            pass
    return fallback


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_on(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """Near-white on dark backgrounds, near-black on light ones."""
    return (15, 23, 42) if _luminance(bg) > 0.55 else (248, 250, 252)


class _Theme:
    def __init__(self, s: PostSettings):
        self.bg = _hex_to_rgb(s.card_bg, BG)
        self.text = _hex_to_rgb(s.card_text, TEXT)
        self.muted = _hex_to_rgb(s.card_muted, MUTED)
        self.accent = _hex_to_rgb(s.card_accent, ACCENT)
        self.head_px = _HEAD_PX.get(s.headline_size, _HEAD_PX["m"])
        self.align = s.headline_align
        self.sign_off = (s.sign_off or "via connect")[:60]


def _draw_block(draw, lines: list[str], font, fill, *, y: int, line_h: int,
                align: str, left: int = MARGIN,
                inner: int = SIZE - 2 * MARGIN) -> int:
    """Draw wrapped lines, left- or center-aligned; returns the new y."""
    for line in lines:
        x = left
        if align == "center":
            x = left + (inner - draw.textlength(line, font=font)) / 2
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y


def _paste_logo(img, logo: bytes | None, *, canvas_w: int = SIZE,
                margin: int = MARGIN) -> None:
    # canvas_w/margin default to the square card; the vertical reel renderer
    # passes its own (1080-wide, larger margin) so the logo lands top-right.
    if not logo:
        return
    try:
        from PIL import Image
        lg = Image.open(io.BytesIO(logo)).convert("RGBA")
        lg.thumbnail((280, 72))
        img.paste(lg, (canvas_w - margin - lg.width, margin - 8), lg)
    except Exception:  # noqa: BLE001 — a bad logo never breaks the card
        pass


def _bullets(draw, points, font, color, accent, *, y: int, align: str,
             dotted: bool) -> int:
    inner = SIZE - 2 * MARGIN
    for kp in [p.strip() for p in points if p.strip()][:4]:
        if dotted:
            draw.ellipse([MARGIN, y + 16, MARGIN + 14, y + 30], fill=accent)
            lines = _wrap(draw, kp, font, inner - 48)[:2]
            y = _draw_block(draw, lines, font, color, y=y, line_h=50,
                            align="left", left=MARGIN + 42, inner=inner - 42)
        else:
            lines = _wrap(draw, kp, font, inner)[:2]
            y = _draw_block(draw, lines, font, color, y=y, line_h=52,
                            align=align)
        y += 16
        if y > SIZE - MARGIN - 80:
            break
    return y


def _new(bg):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (SIZE, SIZE), bg)
    return img, ImageDraw.Draw(img)


_WHITE = (248, 250, 252)
_SOFT = (226, 232, 240)


def _photo_bg(photo: bytes | None, *, scrim: bool = True):
    """Cover-crop ``photo`` to the square canvas, optionally baking in a scrim
    (overall darken + a stronger bottom gradient) so white text reads on any
    image. Returns None when the bytes aren't a usable image — callers fall
    back to the themed solid background. The photo bytes are FETCHED BY THE
    CALLER; this module stays network-free."""
    if not photo:
        return None
    try:
        from PIL import Image, ImageOps
        img = ImageOps.fit(Image.open(io.BytesIO(photo)).convert("RGB"),
                           (SIZE, SIZE), method=Image.LANCZOS)
        if not scrim:
            return img
        col = []
        for yy in range(SIZE):
            ty = yy / SIZE
            a = 0.42
            if ty > 0.55:
                a += 0.30 * (ty - 0.55) / 0.45
            col.append(int(min(0.85, a) * 255))
        alpha = Image.new("L", (1, SIZE))
        alpha.putdata(col)
        alpha = alpha.resize((SIZE, SIZE))
        black = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
        return Image.composite(black, img, alpha)
    except Exception:  # noqa: BLE001 — a bad photo never breaks the card
        return None


def _shadow_block(draw, lines: list[str], font, fill, *, y: int, line_h: int,
                  align: str, left: int = MARGIN,
                  inner: int = SIZE - 2 * MARGIN) -> int:
    """_draw_block with a drop shadow, for text over a photo."""
    for line in lines:
        x = left
        if align == "center":
            x = left + (inner - draw.textlength(line, font=font)) / 2
        draw.text((x + 3, y + 3), line, font=font, fill=(10, 12, 16))
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y


def _render_photo(content, t: _Theme, logo, img):
    """The classic layout over a scrimmed photo — white shadowed text reads
    on any image; the accent bar and logo keep the brand."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, SIZE, 14], fill=t.accent)
    _paste_logo(img, logo)
    inner = SIZE - 2 * MARGIN
    y = MARGIN + 12
    label = (content.source_label or "connect").strip().upper()[:48]
    y = _shadow_block(draw, [label], _font(_BOLD, 30), _WHITE, y=y, line_h=66,
                      align=t.align)
    head_font = _font(_BOLD, t.head_px)
    head_lines = _wrap(draw, content.headline.strip(), head_font, inner)[:5]
    y = _shadow_block(draw, head_lines, head_font, _WHITE, y=y,
                      line_h=int(t.head_px * 1.22), align=t.align)
    y += 34
    _bullets(draw, content.key_points, _font(_REG, 38), _SOFT, t.accent,
             y=y, align=t.align, dotted=True)
    draw.text((MARGIN, SIZE - MARGIN + 6), t.sign_off, font=_font(_REG, 28),
              fill=_SOFT)
    return img


def _render_classic(content, t: _Theme, logo):
    img, draw = _new(t.bg)
    draw.rectangle([0, 0, SIZE, 14], fill=t.accent)
    _paste_logo(img, logo)
    inner = SIZE - 2 * MARGIN
    y = MARGIN + 12
    label = (content.source_label or "connect").strip().upper()[:48]
    _draw_block(draw, [label], _font(_BOLD, 30), t.accent, y=y, line_h=66,
                align=t.align)
    y += 66
    head_font = _font(_BOLD, t.head_px)
    head_lines = _wrap(draw, content.headline.strip(), head_font, inner)[:5]
    y = _draw_block(draw, head_lines, head_font, t.text, y=y,
                    line_h=int(t.head_px * 1.22), align=t.align)
    y += 34
    _bullets(draw, content.key_points, _font(_REG, 38), t.muted, t.accent,
             y=y, align=t.align, dotted=True)
    draw.text((MARGIN, SIZE - MARGIN + 6), t.sign_off, font=_font(_REG, 28),
              fill=t.muted)
    return img


def _render_bold(content, t: _Theme, logo):
    img, draw = _new(t.bg)
    band_h = int(SIZE * 0.46)
    draw.rectangle([0, 0, SIZE, band_h], fill=t.accent)
    on_accent = _contrast_on(t.accent)
    _paste_logo(img, logo)
    inner = SIZE - 2 * MARGIN
    y = MARGIN
    label = (content.source_label or "connect").strip().upper()[:48]
    _draw_block(draw, [label], _font(_BOLD, 30), on_accent, y=y, line_h=58,
                align=t.align)
    y += 64
    head_font = _font(_BOLD, t.head_px)
    head_lines = _wrap(draw, content.headline.strip(), head_font, inner)[:4]
    _draw_block(draw, head_lines, head_font, on_accent, y=y,
                line_h=int(t.head_px * 1.2), align=t.align)
    y = band_h + 54
    _bullets(draw, content.key_points, _font(_REG, 40), t.text, t.accent,
             y=y, align="left", dotted=True)
    draw.text((MARGIN, SIZE - MARGIN + 6), t.sign_off, font=_font(_REG, 28),
              fill=t.muted)
    return img


def _render_minimal(content, t: _Theme, logo):
    img, draw = _new(t.bg)
    _paste_logo(img, logo)
    inner = SIZE - 2 * MARGIN
    y = MARGIN + 8
    label = "  ".join((content.source_label or "connect").strip().upper()[:40])
    _draw_block(draw, [label], _font(_REG, 24), t.muted, y=y, line_h=54,
                align=t.align)
    y += 70
    head_font = _font(_BOLD, t.head_px)
    head_lines = _wrap(draw, content.headline.strip(), head_font, inner)[:5]
    y = _draw_block(draw, head_lines, head_font, t.text, y=y,
                    line_h=int(t.head_px * 1.22), align=t.align)
    # a short accent underline beneath the headline
    ux = MARGIN
    if t.align == "center":
        ux = (SIZE - 120) / 2
    draw.rectangle([ux, y + 14, ux + 120, y + 20], fill=t.accent)
    y += 56
    _bullets(draw, content.key_points, _font(_REG, 38), t.muted, t.accent,
             y=y, align=t.align, dotted=False)
    draw.text((MARGIN, SIZE - MARGIN + 6), t.sign_off, font=_font(_REG, 26),
              fill=t.muted)
    return img


_TEMPLATES = {
    "classic": _render_classic,
    "bold": _render_bold,
    "minimal": _render_minimal,
}


def render_card(content: SocialPost, *, settings: PostSettings,
                logo: bytes | None = None,
                photo: bytes | None = None) -> bytes:
    """Render the post onto a branded square card per the effective
    PostSettings; returns JPEG bytes ready for download or the Graph API.
    ``photo`` (optional raw image bytes, fetched by the caller) becomes a
    scrimmed full-bleed background with white shadowed text."""
    t = _Theme(settings)
    bg = _photo_bg(photo)
    if bg is not None:
        img = _render_photo(content, t, logo, bg)
    else:
        render = _TEMPLATES.get(settings.card_template, _render_classic)
        img = render(content, t, logo)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
