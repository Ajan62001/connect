"""Story synthesis — one DEEP structured call over a CLOSED evidence menu,
then mechanical citation verification (the same discipline as investigation
synthesis): every [[E#]] marker must resolve to a menu id, one regeneration on
failure, then strip + flag the rest. The model never sees anything outside the
menu, so it cannot ground a claim on a source that isn't in the fact-set.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg

from connect.analysis.budget import AnalysisBudget
from connect.analysis.grounding import EvidenceMenu
from connect.llm import spend
from connect.llm.provider import LLMProvider
from connect.llm.tiers import ModelTier
from connect.story.schema import (
    StoryFactSet,
    StoryGrounding,
    StoryOptions,
    StoryOutput,
    StorySource,
)

log = logging.getLogger(__name__)

PURPOSE_STORY_SYNTHESIS = "story_synthesis"
SYN_MAX_TOKENS = 3200
EST_SYN_IN, EST_SYN_OUT = 6000, 1800

CITATION_RE = re.compile(r"\[\[(E\d+)\]\]")

_ARC = (
    "Tell the story with these four movements, as markdown H2 headings, in"
    " order:\n"
    "## What happened\n## What it was (background)\n## Why it happened\n"
    "## How it affects (impact & what to watch)\n")

STORY_SYSTEM = (
    "You are a rigorous newsroom writer. You write a single coherent STORY"
    " strictly from a closed EVIDENCE MENU — a numbered list of facts, each"
    " with an id like E1 and a verbatim quote from a stored source. " + _ARC +
    "\nRULES:\n"
    "- The menu is EVERYTHING you know. Never introduce a fact, name, number,"
    " date or claim that is not supported by a menu item.\n"
    "- In 'What happened' and 'What it was', every factual sentence MUST cite"
    " its supporting item inline as [[E#]] (you may cite several, e.g."
    " [[E2]][[E5]]). Use ONLY ids that appear in the menu.\n"
    "- 'Why it happened' and 'How it affects' are ANALYSIS: reason from the"
    " cited facts, cite the [[E#]] items you build on, and when you go beyond"
    " what the sources state, mark it plainly (e.g. 'this suggests…',"
    " 'analysts would watch…'). Never present interpretation as reported"
    " fact.\n"
    "- Do not invent citation ids. If the menu does not support a movement,"
    " write one honest sentence saying the sources don't cover it.\n"
    "- Write clean markdown prose, not bullet dumps.")

_LENGTH_GUIDE = {
    "brief": "Keep it tight: ~3-5 short paragraphs total across the movements.",
    "standard": "Aim for a concise feature: 1-2 paragraphs per movement.",
    "feature": "Write a full feature: 2-4 paragraphs per movement.",
}
_TONE_GUIDE = {
    "neutral": "Tone: neutral, factual, wire-service plain.",
    "explanatory": "Tone: explanatory and accessible, but never embellished.",
}


def _build_menu(factset: StoryFactSet) -> tuple[EvidenceMenu,
                                                dict[str, Any]]:
    """The closed menu + an id->StoryFact map (for source rendering)."""
    menu = EvidenceMenu()
    by_id: dict[str, Any] = {}
    for fact in factset.facts:
        entry = menu.add(document_id=fact.document_id or 0, quote=fact.quote,
                         source_name=fact.source_name,
                         credibility_tier=fact.credibility_tier)
        by_id[entry.menu_id] = fact
    return menu, by_id


def _cited_ids(text: str) -> list[str]:
    out: list[str] = []
    for m in CITATION_RE.finditer(text or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def _strip(text: str, markers: list[str]) -> str:
    out = text or ""
    for menu_id in markers:
        out = out.replace(f"[[{menu_id}]]", "")
    return out


async def synthesize_story(conn: psycopg.AsyncConnection,
                           provider: LLMProvider, *,
                           factset: StoryFactSet, options: StoryOptions,
                           budget: AnalysisBudget, governor: Any,
                           viewer: int) -> dict[str, Any]:
    menu, by_id = _build_menu(factset)
    style = (options.style or "").strip()
    style_line = (
        f"Style guidance from the reader: {style}. Apply it to voice and"
        " structure ONLY — never relax the citation/grounding rules.\n"
        if style else "")
    instructions = (
        f"SUBJECT: {factset.subject}\n\n{menu.render()}\n\n"
        f"{_LENGTH_GUIDE[options.length]} {_TONE_GUIDE[options.tone]}\n"
        f"{style_line}"
        "Write the story now.")

    model = provider.model_for(ModelTier.DEEP)
    proj = spend.cost_usd(model, input_tokens=EST_SYN_IN,
                          output_tokens=EST_SYN_OUT)
    budget.check(proj)
    await governor.check(proj, user_id=viewer)

    async def _one(extra: str = "") -> StoryOutput:
        completion = await provider.complete_structured(
            system=STORY_SYSTEM,
            messages=[{"role": "user", "content": instructions + extra}],
            schema=StoryOutput, tier=ModelTier.DEEP, max_tokens=SYN_MAX_TOKENS)
        await spend.record_call(conn, purpose=PURPOSE_STORY_SYNTHESIS,
                                model=completion.model, usage=completion.usage,
                                user_id=viewer)
        budget.add(spend.cost_usd(
            completion.model, input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_read_tokens=completion.usage.cache_read_tokens))
        return completion.output

    out = await _one()
    narrative = out.narrative_md
    bad = menu.invalid_ids(_cited_ids(narrative))
    regenerated = False
    if bad:
        # one regeneration with the invalid ids called out
        regenerated = True
        out = await _one(
            "\n\nYour previous draft cited ids that are NOT in the menu: "
            f"{', '.join(bad)}. Rewrite citing ONLY ids that appear above.")
        narrative = out.narrative_md
        bad = menu.invalid_ids(_cited_ids(narrative))
    stripped: list[str] = []
    if bad:                      # still invalid → strip + flag
        narrative = _strip(narrative, bad)
        stripped = bad

    cited = [c for c in _cited_ids(narrative) if c in menu.ids]
    sources = [
        StorySource(
            ref=entry.menu_id, document_id=by_id[entry.menu_id].document_id,
            title=by_id[entry.menu_id].title,
            source_name=by_id[entry.menu_id].source_name,
            quote=by_id[entry.menu_id].quote,
            occurred_on=by_id[entry.menu_id].occurred_on,
            finding_id=by_id[entry.menu_id].finding_id)
        for entry in menu.entries]
    grounding = StoryGrounding(
        menu_size=len(menu.entries), cited_count=len(cited),
        stripped_markers=stripped, regenerated=regenerated)
    return {"title": out.title, "narrative_md": narrative,
            "grounding": grounding.model_dump(),
            "sources": [s.model_dump() for s in sources]}
