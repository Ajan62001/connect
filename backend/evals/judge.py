"""LLM-as-judge for the dimensions without a single gold label — claim
decomposition quality and reasoning faithfulness.

The judge runs on the DEEP tier (the most capable model) and returns a
structured verdict, so grading is itself validated structured output. Judges
are deliberately strict and ask for a one-line rationale, which the report
surfaces for spot-checking. They go through the provider directly (not
EvalContext) since they are not a pipeline stage.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from connect.llm.provider import LLMProvider
from connect.llm.tiers import ModelTier

JUDGE_MAX_TOKENS = 600


class _J(BaseModel):
    model_config = ConfigDict(frozen=True)


class NormalizeJudgment(_J):
    """Scores a decomposition the normalize stage produced."""
    atomicity: float = Field(ge=0.0, le=1.0)          # one proposition / claim
    kind_accuracy: float = Field(ge=0.0, le=1.0)      # factual/causal/...
    checkable_honesty: float = Field(ge=0.0, le=1.0)  # normative != "factual"
    overall_pass: bool
    rationale: str = ""


class ReasoningJudgment(_J):
    """Scores one written verdict-explanation for faithfulness."""
    faithful: bool                # justifies THIS verdict from the menu only
    uses_outside_knowledge: bool  # red flag — should always be False
    quality: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


NORMALIZE_JUDGE_SYSTEM = """You grade the output of a claim-decomposition \
stage for a fact-verification engine. You are given the original INPUT TEXT \
and the CLAIMS the stage extracted (each with a kind and a checkable flag).

Score 0.0-1.0:
- atomicity: are claims self-contained and single-proposition (no pronouns, \
no conjunctions joining separate facts)?
- kind_accuracy: are the kind labels right (factual=is/was, causal=X caused \
Y, normative=should/ought, predictive=about the future)?
- checkable_honesty: are normative/predictive claims flagged checkable=false \
rather than forced into factual? Penalise heavily if a value judgment or \
prediction is marked checkable=true.
overall_pass: true only if the decomposition is faithful and usable.
Be strict; give a one-sentence rationale."""

REASONING_JUDGE_SYSTEM = """You grade one explanation written for an \
ALREADY-DECIDED verdict in a fact-verification engine. You are given the \
CLAIM, the FIXED VERDICT, the closed EVIDENCE MENU (ids like [E1]), and the \
EXPLANATION.

The explanation must justify the given verdict using only the menu evidence.
- faithful: does it support the fixed verdict and stay consistent with the \
menu (not disputing the verdict, not citing anything off-menu)?
- uses_outside_knowledge: does it bring in facts not on the menu? (true is \
bad)
- quality: 0.0-1.0 for clarity and how well it ties evidence to the verdict.
Be strict; give a one-sentence rationale."""


def _normalize_user(input_text: str, claims: list) -> str:
    lines = [f"- [{c.id}] kind={c.kind} checkable={c.checkable}: {c.text}"
             for c in claims]
    body = "\n".join(lines) if lines else "(no claims extracted)"
    head = input_text if len(input_text) < 1500 else input_text[:1500] + " …"
    return f"INPUT TEXT:\n{head}\n\nEXTRACTED CLAIMS:\n{body}"


async def judge_normalize(provider: LLMProvider, *, input_text: str,
                          claims: list) -> NormalizeJudgment:
    completion = await provider.complete_structured(
        system=NORMALIZE_JUDGE_SYSTEM,
        messages=[{"role": "user",
                   "content": _normalize_user(input_text, claims)}],
        schema=NormalizeJudgment, tier=ModelTier.DEEP,
        max_tokens=JUDGE_MAX_TOKENS)
    return completion.output


def _reasoning_user(claim: str, verdict: str, menu_render: str,
                    explanation: str) -> str:
    return (f"CLAIM: {claim}\nFIXED VERDICT: {verdict}\n\n"
            f"EVIDENCE MENU:\n{menu_render}\n\n"
            f"EXPLANATION:\n{explanation}")


async def judge_reasoning(provider: LLMProvider, *, claim: str, verdict: str,
                          menu_render: str,
                          explanation: str) -> ReasoningJudgment:
    completion = await provider.complete_structured(
        system=REASONING_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": _reasoning_user(
            claim, verdict, menu_render, explanation)}],
        schema=ReasoningJudgment, tier=ModelTier.DEEP,
        max_tokens=JUDGE_MAX_TOKENS)
    return completion.output
