"""AnalysisContext — every handle a stage needs, plus the ONE governed LLM
call path: daily Governor check -> per-analysis budget check -> provider ->
spend ledger (purpose 'analysis') -> budget debit. Stages never talk to the
provider or the ledger directly, so no call can escape cost control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import psycopg

from connect.analysis.budget import AnalysisBudget
from connect.analysis.schema import NormalizedInput
from connect.ingestion.pipeline import IngestionPipeline
from connect.knowledge.embedder import Embedder
from connect.knowledge.vector import VectorIndex
from connect.llm import spend
from connect.llm.provider import LLMProvider, StructuredCompletion, TModel
from connect.llm.tiers import ModelTier
from connect.retrieval.search_client import SearchClient

PURPOSE_ANALYSIS = "analysis"

# token-count projection assumptions per call shape (governor look-ahead)
EST_NORMALIZE_IN, EST_NORMALIZE_OUT = 2500, 700
EST_STANCE_IN, EST_STANCE_OUT = 2500, 200
EST_REASONING_IN, EST_REASONING_OUT = 2000, 350
EST_ADJUDICATION_IN, EST_ADJUDICATION_OUT = 600, 50


@dataclass
class AnalysisContext:
    conn: psycopg.AsyncConnection
    provider: LLMProvider
    governor: Any                       # llm.spend.Governor
    budget: AnalysisBudget
    search: SearchClient
    ingest: IngestionPipeline | None
    embedder: Embedder
    vectors: VectorIndex | None
    dossier_id: int
    max_evidence_per_claim: int
    emit: Callable[[str, dict[str, Any]], Awaitable[Any]]  # job_event writer
    normalized: NormalizedInput | None = None
    stage_summaries: dict[str, str] = field(default_factory=dict)

    def projected_cost(self, tier: ModelTier, est_in: int,
                       est_out: int) -> float:
        return spend.cost_usd(self.provider.model_for(tier),
                              input_tokens=est_in, output_tokens=est_out)

    async def call_structured(self, *, system: str, user: str,
                              schema: type[TModel], tier: ModelTier,
                              max_tokens: int, est_in: int, est_out: int,
                              ) -> StructuredCompletion[TModel]:
        """The governed structured call. Raises BudgetExceeded (daily) /
        AnalysisBudgetExceeded (per-analysis) BEFORE spending, LLMError on
        provider failure."""
        projected = self.projected_cost(tier, est_in, est_out)
        await self.governor.check(projected)
        self.budget.check(projected)
        completion = await self.provider.complete_structured(
            system=system, messages=[{"role": "user", "content": user}],
            schema=schema, tier=tier, max_tokens=max_tokens)
        await spend.record_call(self.conn, purpose=PURPOSE_ANALYSIS,
                                model=completion.model,
                                usage=completion.usage)
        self.budget.add(spend.cost_usd(
            completion.model,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cache_read_tokens=completion.usage.cache_read_tokens))
        return completion
