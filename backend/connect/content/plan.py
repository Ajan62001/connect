"""The editorial planner — the orchestrator behind 'auto' campaigns.

One FAST structured LLM call over the CLOSED fact-set decides, like a desk
editor commissioning coverage: how big the story is (significance 1-5), which
formats to produce (reel / card / carousel / thread / LinkedIn post / meme),
and the visual treatment for each (stock photo vs video b-roll, plus a
suggested photographable subject). The model proposes; code disposes —
`_sanitize` enforces the format vocabulary, dedupes, caps the commission by
significance, downgrades video picks when b-roll is unavailable, bounds every
free-text field (they are interpolated into generation prompts and the UI),
and derives `big_news` from the score so the flag can never contradict it.

The planner's menu is a newest-first VIEW of the fact-set (the fact-set handed
to generation is untouched), truncated at entry boundaries with an honest
"showing K of N" count, and prefixed with the recency / corroboration signals
the significance rubric turns on. A planner failure degrades to a one-card
fallback plan instead of failing the whole campaign.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from connect.content import ground
from connect.analysis.budget import AnalysisBudget
from connect.domain.enums import CONTENT_FORMATS
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.tiers import ModelTier
from connect.story.schema import StoryFact, StoryFactSet
from connect.storage.pg import utc_now

log = logging.getLogger(__name__)

PURPOSE = "content_plan"
PLAN_MAX_TOKENS = 1200
EST_IN, EST_OUT = 3500, 400
MAX_PICKS = 4
MENU_CHAR_BUDGET = 6000

# significance -> label (controlled vocabulary; the LLM's label is replaced)
SIGNIFICANCE_LABELS = {5: "breaking", 4: "major", 3: "notable", 2: "minor",
                       1: "routine"}
BIG_NEWS_THRESHOLD = 4

MEDIA_KINDS = ("stock_photo", "stock_video", "none")
TEXT_FORMATS = ("x_thread", "linkedin_post")   # no rendered media

# how many picks a story of each significance may commission — the mechanical
# half of the desk rules (minor: one light format; notable: 1-2; big: 2-4)
PICK_BUDGET = {1: 1, 2: 1, 3: 2, 4: MAX_PICKS, 5: MAX_PICKS}

# free-text caps: these fields ride into generation prompts, jsonb and the UI
MAX_QUERY_CHARS, MAX_QUERY_WORDS = 80, 6
MAX_REASON, MAX_ANGLE, MAX_RATIONALE = 200, 300, 500


class FormatPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(
        description="One of: ig_reel, ig_card, ig_carousel, x_thread,"
                    " linkedin_post, meme.")
    reason: str = Field(
        description="One short sentence: why this format serves this story.")
    media: str = Field(
        default="stock_photo",
        description="The visual treatment: 'stock_video' (moving b-roll —"
                    " reels only), 'stock_photo' (a background photo), or"
                    " 'none' (a clean themed card). Text formats use 'none'.")
    media_query: str = Field(
        default="",
        description="2-4 plain words naming a concrete, photographable"
                    " subject to steer the visuals (e.g. 'Reserve Bank India"
                    " building'). Empty when media is 'none'.")


class EditorialPlan(BaseModel):
    """The commission: how big the story is and what to make of it."""
    model_config = ConfigDict(extra="forbid")
    significance: int = Field(
        ge=1, le=5,
        description="How big this story is right now: 5 breaking/market-"
                    "moving, 4 major development, 3 notable, 2 minor update,"
                    " 1 routine/evergreen.")
    significance_label: str = Field(
        default="",
        description="One word for the score: breaking / major / notable /"
                    " minor / routine.")
    big_news: bool = Field(
        default=False,
        description="True when significance is 4 or 5.")
    angle: str = Field(
        default="",
        description="The one-line editorial angle the content should lead"
                    " with — the single most striking number or stake.")
    rationale: str = Field(
        description="1-2 sentences: why this significance and this mix.")
    picks: list[FormatPick] = Field(
        default_factory=list,
        description="1-4 formats to produce, most important first.")


_SYSTEM = (
    "You are the EDITORIAL PLANNER for a grounded news social-media desk."
    " You see a CLOSED evidence menu (verbatim quotes from stored sources)"
    " about one subject. Decide how big the story is and commission the"
    " coverage — you do not write it.\nRULES:\n"
    "- Judge significance from the evidence only: 5 = breaking, market-moving"
    " or nationally important right now; 4 = major development; 3 = notable,"
    " worth a post; 2 = minor update; 1 = routine or evergreen.\n"
    "- Use the TODAY / DATES / SOURCES lines: a story corroborated by several"
    " outlets and dated within the last day or two rates higher; a"
    " single-outlet or stale story rarely rates above 3.\n"
    "- BIG stories (4-5): commission 2-4 formats and lead with ig_reel"
    " (video travels furthest), usually plus x_thread for speed and ig_card"
    " for the feed.\n"
    "- Notable (3): 1-2 formats. Prefer ig_carousel when the story has 3+"
    " distinct facets or numbers to walk through; else ig_card.\n"
    "- Minor/routine (1-2): exactly ONE light format. Pick meme ONLY when the"
    " story is relatable everyday material AND not grim — never joke about"
    " deaths, disasters, violence or personal tragedy.\n"
    "- linkedin_post only when there is a business, policy or professional"
    " angle.\n"
    "- Media: 'stock_video' only for ig_reel and only when moving footage of"
    " the subject plausibly exists on stock sites{video_note}; otherwise"
    " 'stock_photo'. media_query names a concrete, photographable subject in"
    " 2-4 plain words; 'none' for text formats or a clean themed card.\n"
    "- Never pad the commission — every pick must earn its place.")

_NO_VIDEO = (" (video b-roll is UNAVAILABLE in this deployment — never pick"
             " stock_video)")


# citation-marker-ish tokens ([[E2]], [E2], bare E2). The planner's menu ids
# are renumbered relative to the generation menu (newest-first view), so a
# copied id would point at the WRONG fact downstream — scrub them all.
_MARKER_RE = re.compile(r"\[+\s*E\d+\s*\]+|\bE\d+\b")


def _line(text: str | None, cap: int) -> str:
    """Collapse whitespace and cap length — planner free-text is interpolated
    into downstream prompts, jsonb and the UI, so it must stay one bounded
    line (kills the newline-injection vector into generation prompts) with no
    menu-id tokens (they would mis-cite: the planner's ids aren't
    generation's ids)."""
    return " ".join(_MARKER_RE.sub(" ", text or "").split())[:cap].strip()


def _query(text: str | None) -> str:
    """media_query: a few plain words, nothing more."""
    words = _MARKER_RE.sub(" ", text or "").split()[:MAX_QUERY_WORDS]
    return " ".join(words)[:MAX_QUERY_CHARS].strip()


def _planner_facts(factset: StoryFactSet) -> list[StoryFact]:
    """The planner's LOCAL ordering of the facts: newest first (undated last),
    best credibility tier first within a date — so entry-aligned truncation
    drops the least decision-relevant evidence. The fact-set handed to
    generation is never reordered."""
    facts = sorted(factset.facts,
                   key=lambda f: f.credibility_tier
                   if f.credibility_tier is not None else 99)
    return sorted(facts, key=lambda f: f.occurred_on or "", reverse=True)


def _signal_lines(facts: list[StoryFact]) -> list[str]:
    """The aggregate signals the significance rubric turns on, one line each:
    today's date, the evidence date span, and outlet corroboration + tiers."""
    today = utc_now()[:10]   # ISO date from the one clock
    dated = sorted(f.occurred_on for f in facts if f.occurred_on)
    if dated:
        span = dated[-1] if dated[0] == dated[-1] else \
            f"{dated[0]} to {dated[-1]}"
        dates = f"DATES: evidence dated {span} (newest first below)."
    else:
        dates = "DATES: the evidence carries no dates."
    outlets = {f.source_name for f in facts if f.source_name}
    tiers = Counter(f.credibility_tier for f in facts)
    mix = ", ".join(
        f"{n}×{'tier ' + str(t) if t is not None else 'untiered'}"
        for t, n in sorted(tiers.items(),
                           key=lambda kv: (kv[0] is None, kv[0] or 0)))
    sources = (f"SOURCES: {len(facts)} facts from"
               f" {len(outlets) or 'unknown'} outlet(s) ({mix}).")
    return [f"TODAY: {today}.", dates, sources]


def _menu_text(facts: list[StoryFact]) -> tuple[str, int, int]:
    """The planner's menu, truncated at ENTRY boundaries within the char
    budget — never mid-quote. Returns (text, shown, total) so the prompt can
    say honestly how much of the evidence the model is judging."""
    menu, _by_id = ground.build_menu(
        StoryFactSet(subject="", facts=facts))
    lines = menu.render_lines()
    shown: list[str] = []
    used = 0
    for ln in lines:
        if shown and used + len(ln) + 1 > MENU_CHAR_BUDGET:
            break
        shown.append(ln)
        used += len(ln) + 1
    # pathological first line: keep the menu non-empty, hard-capped
    if shown and len(shown[0]) > MENU_CHAR_BUDGET:
        shown[0] = shown[0][:MENU_CHAR_BUDGET]
    return "\n".join(shown), len(shown), len(lines)


def _fallback_plan(reason: str) -> EditorialPlan:
    """Auto mode degrades instead of failing the campaign: one card, middle
    significance, with the failure recorded in the rationale."""
    return EditorialPlan(
        significance=3,
        rationale=f"planner unavailable ({_line(reason, 160)}) — defaulted"
                  " to a single card",
        picks=[FormatPick(format="ig_card",
                          reason="fallback: planner unavailable",
                          media="stock_photo")])


async def plan_content(conn: psycopg.AsyncConnection, provider: LLMProvider,
                       *, factset: StoryFactSet, budget: AnalysisBudget,
                       governor: Any, viewer: int,
                       video_available: bool = False) -> EditorialPlan:
    """Run the planner over the fact-set and return the SANITIZED plan."""
    facts = _planner_facts(factset)
    menu_text, shown, total = _menu_text(facts)
    system = _SYSTEM.format(video_note="" if video_available else _NO_VIDEO)
    signals = "".join(f"{ln}\n" for ln in _signal_lines(facts))
    user = (f"SUBJECT: {factset.subject}\n{signals}"
            f"EVIDENCE (showing {shown} of {total} facts):\n{menu_text}\n\n"
            "Commission the coverage now.")

    proj = spend.cost_usd(provider.model_for(ModelTier.FAST),
                          input_tokens=EST_IN, output_tokens=EST_OUT)
    budget.check(proj)
    await governor.check(proj, user_id=viewer)
    try:
        completion = await provider.complete_structured(
            system=system, messages=[{"role": "user", "content": user}],
            schema=EditorialPlan, tier=ModelTier.FAST,
            max_tokens=PLAN_MAX_TOKENS)
    except (LLMError, ValidationError) as e:
        # budget/governor denials above propagate (the campaign genuinely
        # cannot proceed) — but a planner blip must not kill the whole run
        log.warning("content planner failed, using fallback plan: %s", e)
        return _sanitize(_fallback_plan(str(e)),
                         video_available=video_available)
    await spend.record_call(conn, purpose=PURPOSE, model=completion.model,
                            usage=completion.usage, user_id=viewer)
    budget.add(spend.cost_usd(
        completion.model, input_tokens=completion.usage.input_tokens,
        output_tokens=completion.usage.output_tokens,
        cache_read_tokens=completion.usage.cache_read_tokens))
    return _sanitize(completion.output, video_available=video_available)


def _sanitize(plan: EditorialPlan, *, video_available: bool) -> EditorialPlan:
    """Enforce the controlled vocabularies and desk rules the LLM only saw in
    prose: known formats only (case-insensitive), deduped, capped by
    significance, text formats forced to media 'none', video downgraded when
    unavailable, no memes on big/grim news, reel leading big stories, every
    free-text field bounded, and the label/flag derived from the score (never
    trusted from the model)."""
    picks: list[FormatPick] = []
    seen: set[str] = set()
    for p in plan.picks:
        fmt = (p.format or "").strip().lower()
        if fmt not in CONTENT_FORMATS or fmt in seen:
            continue
        if fmt == "meme" and plan.significance >= BIG_NEWS_THRESHOLD:
            continue   # big news is never meme material
        seen.add(fmt)
        media = p.media if p.media in MEDIA_KINDS else "stock_photo"
        if media == "stock_video" and (fmt != "ig_reel" or not video_available):
            media = "stock_photo"
        if fmt == "meme":
            media = "stock_photo"   # the photo IS the joke — never clean
        elif fmt in TEXT_FORMATS:
            media = "none"
        picks.append(p.model_copy(update={
            "format": fmt, "media": media,
            "reason": _line(p.reason, MAX_REASON),
            "media_query": _query(p.media_query) if media != "none" else ""}))
    if plan.significance >= BIG_NEWS_THRESHOLD:
        # 'lead with ig_reel' must survive into campaign.formats (the service
        # generates in pick order) — sort BEFORE the budget cut so a reel
        # listed late by the model isn't cut instead of leading
        picks.sort(key=lambda p: p.format != "ig_reel")
    picks = picks[:PICK_BUDGET[plan.significance]]
    if not picks:
        picks = [FormatPick(format="ig_card",
                            reason="fallback: planner returned no usable"
                                   " formats", media="stock_photo")]
    return plan.model_copy(update={
        "picks": picks,
        "angle": _line(plan.angle, MAX_ANGLE),
        "rationale": _line(plan.rationale, MAX_RATIONALE),
        "significance_label": SIGNIFICANCE_LABELS[plan.significance],
        "big_news": plan.significance >= BIG_NEWS_THRESHOLD})
