"""Render an Instagram carousel (cover + one card per slide) to a list of JPEG
bytes. Pure: (CarouselContent, PostSettings) -> [bytes], no network. Shares the
theme (colors/headline align/logo) with the single-card renderer in
connect/social/card.py so a carousel and a card from the same workspace match.
"""

from __future__ import annotations

import io

from connect.content.schema import CarouselContent
from connect.domain.models import PostSettings
from connect.social.card import (
    MARGIN,
    SIZE,
    _BOLD,
    _REG,
    _Theme,
    _draw_block,
    _font,
    _paste_logo,
    _wrap,
)


def _footer(draw, t: _Theme, *, index: int, total: int):
    draw.text((MARGIN, SIZE - MARGIN + 6), t.sign_off, font=_font(_REG, 28),
              fill=t.muted)
    tag = f"{index}/{total}"
    f = _font(_BOLD, 28)
    w = draw.textlength(tag, font=f)
    draw.text((SIZE - MARGIN - w, SIZE - MARGIN + 6), tag, font=f,
              fill=t.accent)


def _new_card(t: _Theme):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (SIZE, SIZE), t.bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, SIZE, 14], fill=t.accent)   # accent bar
    return img, draw


def _to_jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def render_carousel(content: CarouselContent, *, settings: PostSettings,
                    logo: bytes | None = None) -> list[bytes]:
    """Cover slide (source chip + title) then one card per content slide
    (heading + bullets), all themed by the effective PostSettings. Returns
    JPEG bytes per slide, in order."""
    t = _Theme(settings)
    total = len(content.slides) + 1
    cards: list[bytes] = []
    inner = SIZE - 2 * MARGIN

    # -- cover ----------------------------------------------------------------
    img, draw = _new_card(t)
    _paste_logo(img, logo)
    y = MARGIN + 12
    label = (content.source_label or "connect").strip().upper()[:48]
    _draw_block(draw, [label], _font(_BOLD, 30), t.accent, y=y, line_h=80,
                align=t.align)
    y += 80
    head_font = _font(_BOLD, max(t.head_px, 80))
    head_lines = _wrap(draw, content.title.strip(), head_font, inner)[:6]
    _draw_block(draw, head_lines, head_font, t.text, y=y,
                line_h=int(max(t.head_px, 80) * 1.22), align=t.align)
    _footer(draw, t, index=1, total=total)
    cards.append(_to_jpeg(img))

    # -- content slides -------------------------------------------------------
    for i, slide in enumerate(content.slides, start=2):
        img, draw = _new_card(t)
        y = MARGIN + 12
        head_font = _font(_BOLD, 60)
        head_lines = _wrap(draw, slide.heading.strip(), head_font, inner)[:3]
        y = _draw_block(draw, head_lines, head_font, t.text, y=y, line_h=74,
                        align=t.align)
        y += 28
        body_font = _font(_REG, 40)
        for bullet in slide.bullets[:4]:
            bullet = bullet.strip()
            if not bullet:
                continue
            draw.ellipse([MARGIN, y + 18, MARGIN + 14, y + 32], fill=t.accent)
            for line in _wrap(draw, bullet, body_font, inner - 48)[:3]:
                draw.text((MARGIN + 42, y), line, font=body_font, fill=t.muted)
                y += 54
            y += 18
            if y > SIZE - MARGIN - 90:
                break
        _footer(draw, t, index=i, total=total)
        cards.append(_to_jpeg(img))

    return cards
