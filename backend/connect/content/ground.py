"""Grounding primitives for content generation — the SAME discipline as story
synthesis (connect/story/synthesize.py), factored for reuse across the four
formats. Build a closed EvidenceMenu from the fact-set, extract [[E#]] markers
from arbitrary generated text, and resolve the cited menu entries into the
StorySource list that rides on each content item.

Unlike a story narrative (which keeps valid [[E#]] markers inline for the app
to render), social text is published verbatim to a platform that cannot render
a citation marker — so generation strips ALL markers from the published text
and preserves the attribution in the structured ``sources`` list instead.
"""

from __future__ import annotations

import re
from typing import Any

from connect.analysis.grounding import EvidenceMenu
from connect.story.schema import StoryFactSet, StoryGrounding, StorySource

CITATION_RE = re.compile(r"\[\[(E\d+)\]\]")


def build_menu(factset: StoryFactSet) -> tuple[EvidenceMenu, dict[str, Any]]:
    """The closed menu + an id->StoryFact map (for source rendering)."""
    menu = EvidenceMenu()
    by_id: dict[str, Any] = {}
    for fact in factset.facts:
        entry = menu.add(document_id=fact.document_id or 0, quote=fact.quote,
                         source_name=fact.source_name,
                         credibility_tier=fact.credibility_tier)
        by_id[entry.menu_id] = fact
    return menu, by_id


def cited_ids(text: str) -> list[str]:
    """The [[E#]] ids cited in ``text``, order-preserving and de-duplicated."""
    out: list[str] = []
    for m in CITATION_RE.finditer(text or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def strip_all_markers(text: str) -> str:
    """Remove every [[E#]] marker and tidy the doubled spaces it leaves."""
    out = CITATION_RE.sub("", text or "")
    out = re.sub(r" +([.,;:!?])", r"\1", out)   # space before punctuation
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def resolve(menu: EvidenceMenu, by_id: dict[str, Any],
            texts: list[str], *, regenerated: bool) -> tuple[
                list[StorySource], dict[str, Any]]:
    """Given the FINAL generated text fields, compute the cited (valid) menu
    entries as StorySource records and the StoryGrounding summary. Markers are
    expected to have been stripped from what gets published — this works off
    the pre-strip text to know which entries were cited."""
    joined = "\n".join(t for t in texts if t)
    cited = [c for c in cited_ids(joined) if c in menu.ids]
    stripped = menu.invalid_ids(cited_ids(joined))
    # attribute to the entries actually cited; if the model cited nothing
    # valid, fall back to the whole menu so the item is never source-less.
    chosen = cited or [e.menu_id for e in menu.entries]
    sources = [
        StorySource(
            ref=mid,
            document_id=by_id[mid].document_id,
            title=by_id[mid].title,
            source_name=by_id[mid].source_name,
            quote=by_id[mid].quote,
            occurred_on=by_id[mid].occurred_on,
            finding_id=by_id[mid].finding_id)
        for mid in chosen if mid in by_id]
    grounding = StoryGrounding(
        menu_size=len(menu.entries), cited_count=len(cited),
        stripped_markers=stripped, regenerated=regenerated)
    return sources, grounding.model_dump()
