"""Shared prompt construction for social-post generation — used by both the
document social endpoint and the workspace agent's draft tool, so caption
style stays consistent and is driven by the effective PostSettings.
"""

from __future__ import annotations

from typing import Sequence

from connect.domain.models import PostSettings
from connect.social.palettes import palette_guidance

DOC_CAP = 8_000


def system_for(s: PostSettings) -> str:
    """The caption/card system prompt, parameterized by the effective
    post-generation settings (tone, hashtag count, length, brand). When
    auto-theming is on, the model is also asked to suggest a card palette."""
    brand = f" Credit the brand {s.brand_handle}." if s.brand_handle else ""
    theme = f" {palette_guidance()}" if s.auto_theme else ""
    return (
        "You turn ONE news document into an Instagram post. Use ONLY the"
        " supplied document and its enrichment — never invent figures, dates,"
        " or claims. Always credit the source." + brand
        + f" Tone: {s.tone}. Produce a punchy headline, 2-4 short factual key"
        f" points for the image card, a caption of at most"
        f" {s.caption_max_chars} characters that ends by crediting the"
        f" source, and exactly {s.hashtag_count} relevant hashtags." + theme)


def build_prompt(*, title: str | None, source_name: str | None = None,
                 summary: str | None = None,
                 claims: Sequence[str] | None = None,
                 topics: Sequence[str] | None = None,
                 content_text: str | None = None) -> str:
    parts = [f"TITLE: {title or 'untitled'}"]
    if source_name:
        parts.append(f"SOURCE: {source_name}")
    if summary:
        parts.append(f"SUMMARY: {summary}")
    if claims:
        parts.append("KEY CLAIMS:\n"
                     + "\n".join(f"- {c}" for c in list(claims)[:6]))
    if topics:
        parts.append("TOPICS: " + ", ".join(topics))
    parts.append(f"DOCUMENT:\n{(content_text or '')[:DOC_CAP]}")
    return "\n\n".join(parts)
