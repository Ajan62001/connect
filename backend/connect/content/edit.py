"""The reel script EDITOR — a bounded critique->revise loop that raises the
floor on every generated reel before it reaches the renderer.

Generation is one shot; scripts that would ship with a soft hook, saggy
pacing or three near-identical 'city skyline' scenes get caught here. Each
pass: a FAST critique call scores the script (hook / pacing / visuals /
clarity) and either ships it or hands back concrete revision notes; a
BALANCED revision call rewrites over the SAME closed evidence menu. The
citation discipline survives editing: a revision citing ids outside the menu
gets exactly one strict retry, and a still-bad revision is DISCARDED in
favour of the last good draft — the editor can only ever improve a script,
never un-ground it. The whole loop is best-effort: any LLM failure ships the
current draft.

The caller (generate_format) passes its own generation system prompt so the
revision writes under exactly the rules the original draft did.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from connect.analysis.budget import AnalysisBudget, AnalysisBudgetExceeded
from connect.content import ground
from connect.content.schema import ReelContent
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded
from connect.llm.tiers import ModelTier

log = logging.getLogger(__name__)

PURPOSE_CRITIQUE = "reel_editor_critique"
PURPOSE_REVISE = "reel_editor_revise"
CRITIQUE_MAX_TOKENS = 700
REVISE_MAX_TOKENS = 2000
EST_CRIT_IN, EST_CRIT_OUT = 2500, 250
EST_REV_IN, EST_REV_OUT = 5000, 900

MAX_NOTE = 200
SHIP_FLOOR = 4          # every score at/above this ships without notes


class ReelCritique(BaseModel):
    """The editor's verdict on one reel script. Scores are 1-5; 'revise'
    must come with concrete, actionable notes."""
    model_config = ConfigDict(extra="forbid")
    hook: int = Field(
        ge=1, le=5,
        description="Does the title stop the scroll in 2 seconds? 5 = a"
                    " sharp number/stake/question you can't skip; 1 = a"
                    " newspaper headline.")
    pacing: int = Field(
        ge=1, le=5,
        description="Do the scenes move? 5 = every scene earns its place and"
                    " pays off the hook; 1 = filler, repetition, or a flat"
                    " list read aloud.")
    visuals: int = Field(
        ge=1, le=5,
        description="Do the image queries name concrete, DIFFERENT,"
                    " photographable subjects? 5 = varied and specific;"
                    " 1 = abstract or near-duplicate queries.")
    clarity: int = Field(
        ge=1, le=5,
        description="Would a first-time viewer follow the story? 5 ="
                    " instantly; 1 = assumes context the scenes never give.")
    retention: int = Field(
        ge=1, le=5,
        description="Would a viewer stay to the END? 5 = every scene ends"
                    " leaning forward (a tension or 'but/so' turn) and the"
                    " last scene lands a real payoff; 1 = the reel gives"
                    " everything away up front or fizzles out.")
    verdict: str = Field(
        description="'ship' when the script is genuinely strong, 'revise'"
                    " when the notes would make it materially better.")
    notes: list[str] = Field(
        default_factory=list,
        description="1-4 concrete revision instructions (<=200 chars each),"
                    " e.g. \"open with the 42% figure, not the ministry"
                    " name\". Empty when shipping.")


_CRITIQUE_SYSTEM = (
    "You are the supervising EDITOR of a short-video news desk whose reels"
    " compete in the feed against everything else on earth. You review a"
    " draft Instagram Reel script (hook + narrated scenes + on-screen"
    " captions + per-scene image queries) written strictly over a CLOSED"
    " evidence menu.\nJudge CRAFT only — grounding is verified separately"
    " in code:\n"
    "- HOOK: the first 2 seconds decide everything. Demand the single most"
    " surprising number, reversal or stake up front, in spoken language —"
    " if it reads like a newspaper headline, it's a 2.\n"
    "- PACING: ~20-30 seconds spoken; every scene must advance the story."
    " Kill repetition, filler, and bureaucratic phrasing a human wouldn't"
    " say aloud.\n"
    "- VISUALS: image queries must name concrete photographable subjects,"
    " each scene DIFFERENT — no near-duplicate backgrounds.\n"
    "- CLARITY: a viewer with zero context must follow it.\n"
    "- RETENTION: every scene except the last must end leaning forward"
    " (a tension, contrast or 'but/so' turn); the last must land a payoff"
    " — the consequence, who pays, or what happens next. A reel that"
    " gives everything away in scene one is a 2.\n"
    "Hold a high bar: 'ship' only what you would put your name on. But be"
    " decisive — if your notes would not materially improve it, ship it."
    " Never ask for facts beyond the evidence menu.")


def _critique_user(menu_text: str, draft: ReelContent,
                   style_context: str = "") -> str:
    persona = (f"\n\nThe script is written to this brief (voice/form ONLY):\n"
               f"{style_context.strip()}\nJudge it IN CHARACTER — a good hook"
               " for this persona/script-type, not a generic one." )\
        if style_context.strip() else ""
    return (f"EVIDENCE MENU (all the facts the script may use):\n{menu_text}"
            f"{persona}"
            f"\n\nDRAFT SCRIPT:\n{draft.model_dump_json(indent=1)}\n\n"
            "Review it now.")


def _revise_user(menu_text: str, draft: ReelContent,
                 notes: list[str], style_context: str = "") -> str:
    notes_text = "\n".join(f"- {n}" for n in notes)
    persona = (f"\nStay in character (voice/form ONLY):\n"
               f"{style_context.strip()}\n") if style_context.strip() else ""
    return (f"{menu_text}\n\n"
            f"CURRENT SCRIPT:\n{draft.model_dump_json(indent=1)}\n\n"
            "Your editor reviewed this script and requires a revision:\n"
            f"{notes_text}\n{persona}\n"
            "Rewrite the reel script applying every note. Keep everything"
            " that already works; change what the notes name. Same shape,"
            " same evidence discipline: every factual claim cites [[E#]]"
            " from the menu above.")


def _clean_notes(notes: list[str]) -> list[str]:
    out = []
    for n in notes:
        n = " ".join((n or "").split())[:MAX_NOTE].strip()
        if n:
            out.append(n)
    return out[:4]


async def edit_reel(conn: psycopg.AsyncConnection, provider: LLMProvider, *,
                    draft: ReelContent, menu: Any, menu_text: str,
                    gen_system: str, budget: AnalysisBudget, governor: Any,
                    viewer: int, max_passes: int = 2, style_context: str = ""
                    ) -> tuple[ReelContent, dict[str, Any]]:
    """Run the editor loop over ``draft``. Returns the (possibly revised)
    script plus a report dict for the item's grounding payload. Best-effort:
    any LLM/budget failure returns the current draft with the report so far.
    Every accepted revision is citation-clean against ``menu`` (bad ids get
    one strict retry, then the revision is discarded)."""
    report: dict[str, Any] = {"passes": [], "revised": False}
    current = draft
    for _pass in range(max(0, max_passes)):
        try:
            critique = await _critique(conn, provider, current,
                                       menu_text=menu_text, budget=budget,
                                       governor=governor, viewer=viewer,
                                       style_context=style_context)
        except (LLMError, ValidationError, BudgetExceeded,
                AnalysisBudgetExceeded) as e:
            log.info("reel editor critique unavailable (%s); shipping", e)
            break
        notes = _clean_notes(critique.notes)
        scores = {"hook": critique.hook, "pacing": critique.pacing,
                  "visuals": critique.visuals, "clarity": critique.clarity,
                  "retention": critique.retention}
        ship = (critique.verdict or "").strip().lower() != "revise" \
            or not notes or min(scores.values()) >= SHIP_FLOOR
        report["passes"].append(
            {**scores, "verdict": "ship" if ship else "revise",
             "notes": notes})
        if ship:
            break
        try:
            revised = await _revise(conn, provider, current, notes=notes,
                                    menu=menu, menu_text=menu_text,
                                    gen_system=gen_system, budget=budget,
                                    governor=governor, viewer=viewer,
                                    style_context=style_context)
        except (LLMError, ValidationError, BudgetExceeded,
                AnalysisBudgetExceeded) as e:
            log.info("reel editor revision unavailable (%s); shipping", e)
            break
        if revised is None:      # revision could not stay on the menu
            report["passes"][-1]["revision_discarded"] = True
            break
        current = revised
        report["revised"] = True
    return current, report


async def _critique(conn: psycopg.AsyncConnection, provider: LLMProvider,
                    draft: ReelContent, *, menu_text: str,
                    budget: AnalysisBudget, governor: Any,
                    viewer: int, style_context: str = "") -> ReelCritique:
    proj = spend.cost_usd(provider.model_for(ModelTier.FAST),
                          input_tokens=EST_CRIT_IN, output_tokens=EST_CRIT_OUT)
    budget.check(proj)
    await governor.check(proj, user_id=viewer)
    completion = await provider.complete_structured(
        system=_CRITIQUE_SYSTEM,
        messages=[{"role": "user",
                   "content": _critique_user(menu_text, draft,
                                             style_context)}],
        schema=ReelCritique, tier=ModelTier.FAST,
        max_tokens=CRITIQUE_MAX_TOKENS)
    await spend.record_call(conn, purpose=PURPOSE_CRITIQUE,
                            model=completion.model, usage=completion.usage,
                            user_id=viewer)
    budget.add(spend.cost_usd(
        completion.model, input_tokens=completion.usage.input_tokens,
        output_tokens=completion.usage.output_tokens,
        cache_read_tokens=completion.usage.cache_read_tokens))
    return completion.output


async def _revise(conn: psycopg.AsyncConnection, provider: LLMProvider,
                  draft: ReelContent, *, notes: list[str], menu: Any,
                  menu_text: str, gen_system: str, budget: AnalysisBudget,
                  governor: Any, viewer: int,
                  style_context: str = "") -> ReelContent | None:
    """One revision (plus one strict retry on bad citations). None when the
    revision cannot stay on the menu — the caller keeps the prior draft."""

    async def _one(extra: str = "") -> ReelContent:
        proj = spend.cost_usd(provider.model_for(ModelTier.BALANCED),
                              input_tokens=EST_REV_IN,
                              output_tokens=EST_REV_OUT)
        budget.check(proj)
        await governor.check(proj, user_id=viewer)
        completion = await provider.complete_structured(
            system=gen_system,
            messages=[{"role": "user",
                       "content": _revise_user(menu_text, draft, notes,
                                               style_context) + extra}],
            schema=ReelContent, tier=ModelTier.BALANCED,
            max_tokens=REVISE_MAX_TOKENS)
        await spend.record_call(conn, purpose=PURPOSE_REVISE,
                                model=completion.model,
                                usage=completion.usage, user_id=viewer)
        budget.add(spend.cost_usd(
            completion.model, input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_read_tokens=completion.usage.cache_read_tokens))
        return completion.output

    out = await _one()
    bad = menu.invalid_ids(ground.cited_ids(_reel_text(out)))
    if bad:
        out = await _one(
            "\n\nYour previous revision cited ids NOT in the menu: "
            f"{', '.join(bad)}. Rewrite citing ONLY ids that appear above.")
        if menu.invalid_ids(ground.cited_ids(_reel_text(out))):
            return None
    return out


def _reel_text(o: ReelContent) -> str:
    parts = [o.title, o.caption]
    for s in o.scenes:
        parts += [s.narration, s.on_screen_caption, *s.bullets]
    return "\n".join(p for p in parts if p)
