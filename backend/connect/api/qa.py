"""Shared grounded Q&A — one governed, structured LLM call that answers a
free-text question STRICTLY from a supplied context block, returning the
DocumentAnswer shape (answer + grounded + verbatim quote). Used by the
document / story / post "ask" endpoints so they share metering, the budget
gate, and the answer contract."""

from __future__ import annotations

import psycopg
from fastapi import HTTPException

from connect.domain.models import DocumentAnswer
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded
from connect.llm.tiers import ModelTier

QA_MAX_TOKENS = 700
QA_EST_OUT_TOKENS = 500
QA_CONTENT_CAP = 30_000


async def grounded_answer(provider: LLMProvider, governor, conn:
                          psycopg.AsyncConnection, *, system: str,
                          context: str, question: str, user_id: int,
                          purpose: str) -> DocumentAnswer:
    """Answer ``question`` from ``context`` only. Raises HTTPException 429
    (budget) / 502 (LLM error). Caller handles 404/503/422 first."""
    ctx = context
    if len(ctx) > QA_CONTENT_CAP:
        ctx = ctx[:QA_CONTENT_CAP] + "\n…[truncated]"
    user_prompt = f"QUESTION:\n{question}\n\n{ctx}"
    est_in = (len(system) + len(user_prompt)) // 4 + 200
    projected = spend.cost_usd(
        provider.model_for(ModelTier.BALANCED),
        input_tokens=est_in, output_tokens=QA_EST_OUT_TOKENS)
    try:
        await governor.check(projected, user_id=user_id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        completion = await provider.complete_structured(
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            schema=DocumentAnswer, tier=ModelTier.BALANCED,
            max_tokens=QA_MAX_TOKENS)
    except LLMError as e:
        raise HTTPException(status_code=502,
                            detail=f"could not answer: {e}") from e
    await spend.record_call(conn, purpose=purpose, model=completion.model,
                            usage=completion.usage, user_id=user_id)
    return completion.output
