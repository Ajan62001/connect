"""Natural-language entailment scorer — the semantic half of the publish-time
editorial gate (S2).

The structural trust core guarantees a published sentence can only CITE a quote
that exists on the closed evidence menu; it does NOT guarantee the sentence is
actually SUPPORTED BY that quote. This module closes that gap: given a premise
(the verbatim source quotes an item is grounded in) and a hypothesis (one
generated sentence), it returns whether the hypothesis is entailed.

It is a FAST-tier, budget-governed structured call — a near-verbatim copy of the
`writeback._same_proposition` scaffolding, NOT the heavier analysis stance
classifier and NOT the offline evals faithfulness judge (which stays the
gold-free regression grader in S5). Callers own fail-open semantics: a provider
or budget error must never block publishing, so they treat a raised error as
"unknown / do not flag".
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field

from connect.analysis.budget import AnalysisBudget
from connect.llm import spend
from connect.llm.provider import LLMProvider
from connect.llm.tiers import ModelTier

PURPOSE = "content_verify"
EST_IN, EST_OUT = 700, 64

ENTAILMENT_SYSTEM = (
    "You are a strict fact-checker. You are given EVIDENCE (verbatim quotes from"
    " stored sources) and one STATEMENT from a draft post. Decide whether the"
    " STATEMENT is supported by the EVIDENCE.\n"
    "- 'entailed': every factual assertion in the statement (names, numbers,"
    " dates, quantities, causal/again claims) follows from the evidence. Numbers"
    " expressed differently but equal (e.g. '₹2 lakh crore' == '2,00,000"
    " crore', '7.5 per cent' == '7.5%') count as supported.\n"
    "- 'not_entailed': the statement asserts a fact ABSENT from or CONTRADICTED"
    " by the evidence (a fabricated/changed number, an unsupported attribution,"
    " a claim the evidence does not make).\n"
    "- 'partial': the gist is supported but a secondary detail is unsupported.\n"
    "Pure opinion/framing with no checkable fact is 'entailed'. Judge ONLY"
    " against the evidence given; never use outside knowledge.")

Label = Literal["entailed", "not_entailed", "partial"]


class EntailmentJudgment(BaseModel):
    label: Label
    rationale: str = Field(default="", max_length=300)


async def score(conn: psycopg.AsyncConnection, provider: LLMProvider, *,
                evidence: str, statement: str, budget: AnalysisBudget,
                governor: Any, viewer: int | None = None) -> EntailmentJudgment:
    """Govern-checked FAST entailment of ``statement`` against ``evidence``.

    Raises (BudgetExceeded / AnalysisBudgetExceeded / LLMError) on cost or
    provider failure — the caller decides fail-open. Cost is recorded under the
    ``content_verify`` purpose so the gate is visible in the spend ledger.
    """
    proj = spend.cost_usd(provider.model_for(ModelTier.FAST),
                          input_tokens=EST_IN, output_tokens=EST_OUT)
    await governor.check(proj, user_id=viewer)
    budget.check(proj)
    user = (f"EVIDENCE:\n{evidence}\n\nSTATEMENT: {statement}\n\n"
            "Is the STATEMENT supported by the EVIDENCE?")
    completion = await provider.complete_structured(
        system=ENTAILMENT_SYSTEM,
        messages=[{"role": "user", "content": user}],
        schema=EntailmentJudgment, tier=ModelTier.FAST, max_tokens=128)
    await spend.record_call(conn, purpose=PURPOSE, model=completion.model,
                            usage=completion.usage, user_id=viewer)
    budget.add(spend.cost_usd(
        completion.model, input_tokens=completion.usage.input_tokens,
        output_tokens=completion.usage.output_tokens,
        cache_read_tokens=completion.usage.cache_read_tokens))
    return completion.output
