"""Grounded generation of one social-content format over a CLOSED fact-set.

One structured LLM call per format, written over the evidence menu with the
[[E#]] citation discipline; cited ids are verified in code, regenerated once on
failure, then ALL markers are stripped from the published text (the source
attribution is preserved in the StorySource list). The same trust gate as story
synthesis, applied to four output shapes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg
from pydantic import BaseModel

from connect.analysis.budget import AnalysisBudget
from connect.content import gate, ground
from connect.content.schema import (
    CarouselContent,
    ContentOptions,
    FORMAT_SCHEMA,
    LinkedInContent,
    MemeContent,
    ReelContent,
    ThreadContent,
)
from connect.domain.enums import CONTENT_FORMAT_PLATFORM
from connect.domain.models import PostSettings, SocialPost
from connect.social import palettes
from connect.llm import spend
from connect.llm.provider import LLMProvider
from connect.llm.tiers import ModelTier
from connect.story.schema import StoryFactSet, StorySource

log = logging.getLogger(__name__)

PURPOSE = "content_generate"
GEN_MAX_TOKENS = 2000
EST_IN, EST_OUT = 4500, 900

_SYSTEM_BASE = (
    "You turn a CLOSED set of facts into a social-media post for {platform}."
    " You see an EVIDENCE MENU: numbered facts (E1, E2, …), each a verbatim"
    " quote from a stored source.\nRULES:\n"
    "- The menu is EVERYTHING you know. Never introduce a fact, name, number,"
    " date or claim that is not supported by a menu item.\n"
    "- Every factual claim MUST cite its supporting item inline as [[E#]]"
    " (you may cite several, e.g. [[E2]][[E5]]). Use ONLY ids that appear in"
    " the menu — never invent an id.\n"
    "- Be punchy and accurate. Never sensationalize, editorialize beyond the"
    " facts, or fabricate engagement bait. Always credit the source.\n")

_FORMAT_GUIDE = {
    "ig_card": (
        "Produce ONE Instagram card: a short factual headline, an engaging"
        " 1-3 sentence caption ending in a source credit, 2-4 very short card"
        " bullet points, and relevant hashtags."),
    "ig_carousel": (
        "Produce an Instagram CAROUSEL: a cover title, then {slides} content"
        " slides each with a heading and 1-3 short factual lines, plus a"
        " caption and hashtags. Tell a small arc across the slides."),
    "x_thread": (
        "Produce an X THREAD of about {tweets} tweets (each <=270 chars):"
        " a hook first, one idea per tweet, the last crediting the source."),
    "linkedin_post": (
        "Produce ONE LinkedIn post: 2-5 short paragraphs, explanatory and"
        " accessible, ending with a source credit."),
    "ig_reel": (
        "Produce a FAST, punchy Instagram REEL built to STOP THE SCROLL. Open"
        " with a HOOK (the title) that lands in the first 2 seconds: lead with"
        " the single most surprising NUMBER or highest-stakes claim, as a sharp"
        " question or bold stake (aim for <=8 words). Make a viewer feel they'll"
        " miss something — punchy, never clickbait-false."
        " Then EXACTLY {scenes} tight scenes that pay off the hook. Keep it"
        " SHORT — the whole thing should run about 20-30 seconds when read"
        " aloud. Each scene: ONE short spoken NARRATION sentence (conversational,"
        " no filler), a SHORT punchy on-screen caption (a few big words, not a"
        " sentence), and an IMAGE_QUERY of 2-4 plain words naming a concrete,"
        " photographable subject for the background photo. End with a caption"
        " and hashtags. Energetic and visual — never a bulleted summary."),
    "meme": (
        "Produce a NEWS MEME: a relevant stock PHOTO (image_query: 2-4 plain"
        " words naming a concrete, photographable subject that carries the"
        " joke) with a big TOP_TEXT setup and BOTTOM_TEXT punchline, plus a"
        " caption giving the real, factual news. The humor comes from FRAMING"
        " — relatability, irony of the situation, shared everyday frustration"
        " — NEVER from invented facts, mockery of victims, or a tragedy played"
        " for laughs (if the story is grim, skip the joke and be wry instead)."
        " Any number or factual claim in the top/bottom text must be supported"
        " by the menu; keep both lines short and punchy."),
}


@dataclass
class FormatResult:
    fmt: str
    platform: str
    content: BaseModel
    sources: list[StorySource]
    grounding: dict[str, Any]
    gate: dict[str, Any] = field(default_factory=dict)


# -- per-format text accessors (what to verify; what to strip) -----------------

def _texts(fmt: str, o: BaseModel) -> list[str]:
    if isinstance(o, SocialPost):
        return [o.headline, o.caption, *o.key_points]
    if isinstance(o, CarouselContent):
        out = [o.title, o.caption]
        for s in o.slides:
            out += [s.heading, *s.bullets]
        return out
    if isinstance(o, ReelContent):
        out = [o.title, o.caption]
        for s in o.scenes:
            out += [s.narration, s.on_screen_caption, *s.bullets]
        return out
    if isinstance(o, ThreadContent):
        return list(o.tweets)
    if isinstance(o, LinkedInContent):
        return [o.body]
    if isinstance(o, MemeContent):
        return [o.top_text, o.bottom_text, o.caption]
    return []


def _strip(o: BaseModel) -> BaseModel:
    s = ground.strip_all_markers
    if isinstance(o, SocialPost):
        return o.model_copy(update={
            "headline": s(o.headline), "caption": s(o.caption),
            "key_points": [s(k) for k in o.key_points]})
    if isinstance(o, CarouselContent):
        return o.model_copy(update={
            "title": s(o.title), "caption": s(o.caption),
            "slides": [sl.model_copy(update={
                "heading": s(sl.heading),
                "bullets": [s(b) for b in sl.bullets]})
                for sl in o.slides]})
    if isinstance(o, ReelContent):
        return o.model_copy(update={
            "title": s(o.title), "caption": s(o.caption),
            "scenes": [sc.model_copy(update={
                "narration": s(sc.narration),
                "on_screen_caption": s(sc.on_screen_caption),
                "bullets": [s(b) for b in sc.bullets]})
                for sc in o.scenes]})
    if isinstance(o, ThreadContent):
        return o.model_copy(update={"tweets": [s(t) for t in o.tweets]})
    if isinstance(o, LinkedInContent):
        return o.model_copy(update={"body": s(o.body)})
    if isinstance(o, MemeContent):
        return o.model_copy(update={
            "top_text": s(o.top_text), "bottom_text": s(o.bottom_text),
            "caption": s(o.caption)})
    return o


# citation-marker-ish tokens: the planner sees a differently-numbered menu,
# so an E# it echoes would mis-cite here — scrub at the sink too
_MARKER_RE = re.compile(r"\[+\s*E\d+\s*\]+|\bE\d+\b")


def _hint(text: str | None, cap: int = 80) -> str:
    """One bounded, marker-free line for planner-provided free text — it is
    model output over scraped sources, so it never gets to add its own prompt
    lines or smuggle citation ids."""
    return " ".join(_MARKER_RE.sub(" ", text or "").split())[:cap].strip()


def _build_user_prompt(fmt: str, menu_text: str, factset: StoryFactSet,
                       options: ContentOptions, settings: PostSettings,
                       media_hint: str | None = None,
                       angle: str | None = None,
                       pick_reason: str | None = None) -> str:
    guide = _FORMAT_GUIDE[fmt].format(slides=options.slide_count,
                                      tweets=options.thread_length,
                                      scenes=options.scene_count)
    style = (options.style or "").strip()
    style_line = (f"Style guidance: {style}. Apply to voice ONLY — never relax"
                  " the citation rules.\n" if style else "")
    brand = (f"Credit the brand {settings.brand_handle}.\n"
             if settings.brand_handle else "")
    theme = (f"{palettes.palette_guidance()}\n"
             if settings.auto_theme
             and fmt in ("ig_card", "ig_carousel", "ig_reel")
             else "")
    hint = _hint(media_hint)
    media = (f"Editorial visual direction (VISUALS ONLY — not evidence):"
             f" build image queries around '{hint}' where they fit the"
             " content.\n" if hint else "")
    lead = _hint(angle, 300)
    angle_line = (f"Editorial angle — LEAD with this: {lead}. Framing ONLY,"
                  " not evidence: every factual claim still cites [[E#]].\n"
                  if lead else "")
    why = _hint(pick_reason, 200)
    reason_line = (f"Why this format was commissioned: {why}\n" if why else "")
    return (
        f"SUBJECT: {factset.subject}\n\n{menu_text}\n\n"
        f"{guide}\nTone: {options.tone}. About {options.hashtag_count}"
        f" hashtags (without '#').\n{style_line}{brand}{theme}"
        f"{angle_line}{reason_line}{media}"
        "Write it now, citing [[E#]] for every factual claim.")


async def generate_format(conn: psycopg.AsyncConnection,
                          provider: LLMProvider, *, fmt: str,
                          factset: StoryFactSet, options: ContentOptions,
                          settings: PostSettings, budget: AnalysisBudget,
                          governor: Any, viewer: int,
                          media_hint: str | None = None,
                          angle: str | None = None,
                          pick_reason: str | None = None) -> FormatResult:
    schema = FORMAT_SCHEMA[fmt]
    platform = CONTENT_FORMAT_PLATFORM[fmt]
    menu, by_id = ground.build_menu(factset)
    menu_text = menu.render()
    system = _SYSTEM_BASE.format(platform=platform)
    user = _build_user_prompt(fmt, menu_text, factset, options, settings,
                              media_hint=media_hint, angle=angle,
                              pick_reason=pick_reason)

    proj = spend.cost_usd(provider.model_for(ModelTier.BALANCED),
                          input_tokens=EST_IN, output_tokens=EST_OUT)
    budget.check(proj)
    await governor.check(proj, user_id=viewer)

    async def _one(extra: str = "") -> BaseModel:
        completion = await provider.complete_structured(
            system=system,
            messages=[{"role": "user", "content": user + extra}],
            schema=schema, tier=ModelTier.BALANCED, max_tokens=GEN_MAX_TOKENS)
        await spend.record_call(conn, purpose=PURPOSE, model=completion.model,
                                usage=completion.usage, user_id=viewer)
        budget.add(spend.cost_usd(
            completion.model, input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_read_tokens=completion.usage.cache_read_tokens))
        return completion.output

    out = await _one()
    bad = menu.invalid_ids(ground.cited_ids("\n".join(_texts(fmt, out))))
    regenerated = False
    if bad:
        regenerated = True
        out = await _one(
            "\n\nYour previous draft cited ids NOT in the menu: "
            f"{', '.join(bad)}. Rewrite citing ONLY ids that appear above.")

    # resolve attribution from the (possibly regenerated) text BEFORE stripping
    sources, grounding = ground.resolve(
        menu, by_id, _texts(fmt, out), regenerated=regenerated)

    # S2 editorial gate: verify the generated prose is actually entailed by the
    # evidence it is attributed to (warn-only; fails open on any LLM/budget
    # error so generation is never blocked by the verifier).
    report = await gate.assess(
        conn, provider, content_texts=_texts(fmt, out),
        source_quotes=[s.quote for s in sources if s.quote],
        document_ids=[s.document_id for s in sources if s.document_id],
        budget=budget, governor=governor, viewer=viewer)

    published = _strip(out)
    return FormatResult(fmt=fmt, platform=platform, content=published,
                        sources=sources, grounding=grounding,
                        gate=report.model_dump())
