"""Render a 1080×1080 Instagram card from a SocialPost (Pillow).

Pure: SocialPost -> JPEG bytes, no network. Fonts are loaded from common
Linux paths with a graceful fallback to Pillow's built-in bitmap font, so
rendering never hard-fails on a font-less box (it just looks plainer).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

from connect.domain.models import SocialPost

SIZE = 1080
MARGIN = 84
BG = (15, 23, 42)        # slate-900
ACCENT = (56, 189, 248)  # sky-400
TEXT = (241, 245, 249)   # slate-100
MUTED = (148, 163, 184)  # slate-400

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


def render_card(content: SocialPost) -> bytes:
    """Render the post's headline + key points + source onto a branded square
    card; returns JPEG bytes ready for download or the Instagram Graph API."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, SIZE, 14], fill=ACCENT)   # accent bar

    inner = SIZE - 2 * MARGIN
    y = MARGIN + 12

    # source chip
    label = (content.source_label or "connect").strip().upper()[:48]
    draw.text((MARGIN, y), label, font=_font(_BOLD, 30), fill=ACCENT)
    y += 66

    # headline (wrapped, bounded so the card never overflows)
    head_font = _font(_BOLD, 74)
    for line in _wrap(draw, content.headline.strip(), head_font, inner)[:5]:
        draw.text((MARGIN, y), line, font=head_font, fill=TEXT)
        y += 90

    y += 34
    # key points
    body_font = _font(_REG, 38)
    for kp in content.key_points[:4]:
        kp = kp.strip()
        if not kp:
            continue
        draw.ellipse([MARGIN, y + 16, MARGIN + 14, y + 30], fill=ACCENT)
        for line in _wrap(draw, kp, body_font, inner - 48)[:2]:
            draw.text((MARGIN + 42, y), line, font=body_font, fill=MUTED)
            y += 50
        y += 16
        if y > SIZE - MARGIN - 80:
            break

    draw.text((MARGIN, SIZE - MARGIN + 6), "via connect",
              font=_font(_REG, 28), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
