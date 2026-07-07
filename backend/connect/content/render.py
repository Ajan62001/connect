"""Render an Instagram carousel (cover + one card per slide) and the meme
format to JPEG bytes. Pure: (content, PostSettings[, photo bytes]) -> bytes,
no network — background photos are FETCHED BY THE CALLER (see
ContentService._render) and passed in as raw bytes. Shares the theme
(colors/headline align/logo) with the single-card renderer in
connect/social/card.py so a carousel and a card from the same workspace match.
"""

from __future__ import annotations

import io
from typing import Sequence

from connect.content.schema import CarouselContent, MemeContent
from connect.domain.models import PostSettings
from connect.social.card import (
    MARGIN,
    SIZE,
    _BOLD,
    _REG,
    _SOFT,
    _WHITE,
    _Theme,
    _draw_block,
    _fit_into,
    _font,
    _open_photo,
    _paste_logo,
    _photo_bg,
    _poster_bg,
    _poster_caption,
    _shadow_block,
    _wrap,
)


def _footer(draw, t: _Theme, *, index: int, total: int, on_photo: bool = False):
    muted = _SOFT if on_photo else t.muted
    draw.text((MARGIN, SIZE - MARGIN + 6), t.sign_off, font=_font(_REG, 28),
              fill=muted)
    tag = f"{index}/{total}"
    f = _font(_BOLD, 28)
    w = draw.textlength(tag, font=f)
    draw.text((SIZE - MARGIN - w, SIZE - MARGIN + 6), tag, font=f,
              fill=_WHITE if on_photo else t.accent)


def _new_card(t: _Theme, photo: bytes | None = None):
    """A fresh square canvas: the scrimmed photo when one is given (and
    usable), else the themed solid. Returns (img, draw, on_photo)."""
    from PIL import Image, ImageDraw
    img = _photo_bg(photo)
    on_photo = img is not None
    if img is None:
        img = Image.new("RGB", (SIZE, SIZE), t.bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, SIZE, 14], fill=t.accent)   # accent bar
    return img, draw, on_photo


def _to_jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _fit_photo_below(img, photo: bytes | None, *, y_top: int) -> None:
    """Fitted style: contain the WHOLE photo (never cropped) in the space
    between ``y_top`` and the footer on the solid card. No-op when the bytes
    are unusable or the leftover space is too thin to be worth it."""
    im = _open_photo(photo)
    if im is None:
        return
    box = (MARGIN, y_top, SIZE - MARGIN, SIZE - MARGIN - 24)
    if box[3] - box[1] >= 200:
        _fit_into(im, img, box=box)


def render_carousel(content: CarouselContent, *, settings: PostSettings,
                    logo: bytes | None = None,
                    photos: Sequence[bytes | None] | None = None
                    ) -> list[bytes]:
    """Cover slide (source chip + title) then one card per content slide
    (heading + bullets), all themed by the effective PostSettings. Returns
    JPEG bytes per slide, in order. ``photos`` (cover first, then one per
    slide; None entries just leave the themed solid) style per
    card_photo_style: 'poster' (default) gives the COVER slide the full-bleed
    photo + bottom accent caption (content slides keep the scrimmed-photo
    treatment); 'fitted' contains each photo whole on the solid theme colour
    below the text; 'cover' is the legacy scrimmed full-bleed."""
    t = _Theme(settings)
    fitted = t.photo_style == "fitted"
    poster = t.photo_style == "poster"
    total = len(content.slides) + 1
    cards: list[bytes] = []
    inner = SIZE - 2 * MARGIN

    def _photo_at(idx: int) -> bytes | None:
        return photos[idx] if photos and idx < len(photos) else None

    # -- cover ----------------------------------------------------------------
    poster_bg = _poster_bg(_photo_at(0)) if poster else None
    if poster_bg is not None:
        from PIL import ImageDraw
        img, draw = poster_bg, ImageDraw.Draw(poster_bg)
        _paste_logo(img, logo, left=True)
        _poster_caption(draw, content.title, t, bottom=SIZE - 110,
                        source_label=content.source_label)
        _footer(draw, t, index=1, total=total, on_photo=True)
    else:
        img, draw, on_photo = _new_card(t, None if (fitted or poster)
                                        else _photo_at(0))
        _paste_logo(img, logo)
        y = MARGIN + 12
        label = (content.source_label or "connect").strip().upper()[:48]
        block = _shadow_block if on_photo else _draw_block
        block(draw, [label], _font(_BOLD, 30),
              _WHITE if on_photo else t.accent, y=y, line_h=80, align=t.align)
        y += 80
        head_font = _font(_BOLD, max(t.head_px, 80))
        head_lines = _wrap(draw, content.title.strip(), head_font, inner)[:6]
        y = block(draw, head_lines, head_font, _WHITE if on_photo else t.text,
                  y=y, line_h=int(max(t.head_px, 80) * 1.22), align=t.align)
        if fitted:
            _fit_photo_below(img, _photo_at(0), y_top=y + 36)
        _footer(draw, t, index=1, total=total, on_photo=on_photo)
    cards.append(_to_jpeg(img))

    # -- content slides -------------------------------------------------------
    for i, slide in enumerate(content.slides, start=2):
        img, draw, on_photo = _new_card(t, None if fitted else _photo_at(i - 1))
        block = _shadow_block if on_photo else _draw_block
        y = MARGIN + 12
        head_font = _font(_BOLD, 60)
        head_lines = _wrap(draw, slide.heading.strip(), head_font, inner)[:3]
        y = block(draw, head_lines, head_font,
                  _WHITE if on_photo else t.text, y=y, line_h=74,
                  align=t.align)
        y += 28
        body_font = _font(_REG, 40)
        body_fill = _SOFT if on_photo else t.muted
        for bullet in slide.bullets[:4]:
            bullet = bullet.strip()
            if not bullet:
                continue
            draw.ellipse([MARGIN, y + 18, MARGIN + 14, y + 32], fill=t.accent)
            for line in _wrap(draw, bullet, body_font, inner - 48)[:3]:
                draw.text((MARGIN + 42, y), line, font=body_font,
                          fill=body_fill)
                y += 54
            y += 18
            if y > SIZE - MARGIN - 90:
                break
        if fitted:
            _fit_photo_below(img, _photo_at(i - 1), y_top=y + 24)
        _footer(draw, t, index=i, total=total, on_photo=on_photo)
        cards.append(_to_jpeg(img))

    return cards


def render_meme(content: MemeContent, *, settings: PostSettings,
                logo: bytes | None = None,
                photo: bytes | None = None) -> bytes:
    """The classic meme treatment: the photo full-bleed (no scrim — the heavy
    text stroke carries readability), uppercase white top/bottom lines with a
    thick black outline, and a small source credit under the punchline. Falls
    back to the themed solid background when no photo could be fetched."""
    from PIL import Image, ImageDraw
    t = _Theme(settings)
    img = _photo_bg(photo, scrim=False)
    if img is None:
        img = Image.new("RGB", (SIZE, SIZE), t.bg)
    draw = ImageDraw.Draw(img)
    # no logo on memes: the top text band owns the full width, and the small
    # source credit below the punchline carries attribution.

    pad = 48

    def _block(text: str, *, top: bool) -> None:
        text = (text or "").strip().upper()
        if not text:
            return
        # shrink to fit: big is the meme look, but a schema-max (90-char)
        # line must never be cut mid-sentence — smaller sizes earn more lines
        for px, max_lines in ((84, 2), (68, 3), (54, 4), (44, 4)):
            font = _font(_BOLD, px)
            lines = _wrap(draw, text, font, SIZE - 2 * pad)
            if len(lines) <= max_lines:
                break
        lines = lines[:max_lines]
        line_h = int(px * 1.16)
        stroke = max(3, px // 14)
        y = pad if top else SIZE - pad - 30 - line_h * len(lines)
        for line in lines:
            x = (SIZE - draw.textlength(line, font=font)) / 2
            draw.text((x, y), line, font=font, fill=(255, 255, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0))
            y += line_h

    _block(content.top_text, top=True)
    _block(content.bottom_text, top=False)

    label = (content.source_label or "").strip()
    if label:
        f = _font(_REG, 26)
        w = draw.textlength(label, font=f)
        draw.text(((SIZE - w) / 2, SIZE - pad + 4), label, font=f,
                  fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))

    return _to_jpeg(img)
