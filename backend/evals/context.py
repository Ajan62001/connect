"""EvalContext — the minimal surface the analysis stages actually need.

``normalize.run``, ``verify.stance_one``, and ``verify.write_reasoning`` each
touch the context only through ``call_structured``. EvalContext provides that
one method (delegating straight to the provider, no governor / budget / ledger)
so the eval harness runs the REAL stage code — same prompts, same schemas, same
verbatim-span and off-menu retries — against any LLMProvider.
"""

from __future__ import annotations

from connect.llm.provider import LLMProvider, StructuredCompletion, TModel
from connect.llm.tiers import ModelTier


class EvalContext:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    def projected_cost(self, tier: ModelTier, est_in: int,
                       est_out: int) -> float:
        return 0.0   # evals are ungoverned by design

    async def call_structured(self, *, system: str, user: str,
                              schema: type[TModel], tier: ModelTier,
                              max_tokens: int, est_in: int, est_out: int,
                              ) -> StructuredCompletion[TModel]:
        return await self.provider.complete_structured(
            system=system, messages=[{"role": "user", "content": user}],
            schema=schema, tier=tier, max_tokens=max_tokens)
