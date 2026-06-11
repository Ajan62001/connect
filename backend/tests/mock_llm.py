"""Test doubles for the LLM layer — NO network, NO anthropic SDK calls.

MockProvider records every call and returns canned outputs; MockBatchRunner
records submissions and plays results back instantly through the real
ingest path. Default usage mirrors the T1 cost-model assumptions (2200 in /
300 out) so governor math in tests matches production projections.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from connect.llm.batch_runner import BatchItem, BatchResult, BatchRunner
from connect.llm.provider import (
    Completion,
    LLMProvider,
    StructuredCompletion,
    TModel,
    Usage,
)
from connect.llm.tiers import DEFAULT_TIER_MODELS, ModelTier

DEFAULT_USAGE = Usage(input_tokens=2200, output_tokens=300)


class MockProvider(LLMProvider):
    """``respond`` is either a fixed BaseModel instance (always returned) or
    a callable (user_text) -> BaseModel. ``respond_by_schema`` maps a
    requested schema class to its own responder (instance or callable) —
    Phase 2 flows mix schemas (T1 extraction, clustering adjudications,
    event summaries, follow-up judgments) in one run; unmapped schemas fall
    back to ``respond``."""

    def __init__(self, respond: Any = None, *, usage: Usage = DEFAULT_USAGE,
                 text: str = "ok",
                 respond_by_schema: dict[type, Any] | None = None):
        self.respond = respond
        self.respond_by_schema = respond_by_schema or {}
        self.usage = usage
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def model_for(self, tier: ModelTier) -> str:
        return DEFAULT_TIER_MODELS[tier]

    async def complete(self, *, system: str | None,
                       messages: Sequence[dict[str, Any]],
                       tier: ModelTier, max_tokens: int) -> Completion:
        self.calls.append({"kind": "complete", "system": system,
                           "messages": list(messages), "tier": tier,
                           "max_tokens": max_tokens})
        return Completion(text=self.text, model=self.model_for(tier),
                          usage=self.usage)

    async def complete_structured(self, *, system: str | None,
                                  messages: Sequence[dict[str, Any]],
                                  schema: type[TModel], tier: ModelTier,
                                  max_tokens: int,
                                  ) -> StructuredCompletion[TModel]:
        user_text = messages[-1]["content"] if messages else ""
        self.calls.append({"kind": "structured", "system": system,
                           "user_text": user_text, "schema": schema,
                           "tier": tier, "max_tokens": max_tokens})
        responder = self.respond_by_schema.get(schema, self.respond)
        out = responder(user_text) if callable(responder) else responder
        assert out is not None, \
            f"MockProvider.respond not configured for {schema.__name__}"
        return StructuredCompletion[schema](  # type: ignore[valid-type]
            output=out, model=self.model_for(tier), usage=self.usage)


class MockBatchRunner(BatchRunner):
    """Plays one canned result per submitted item; 'ends' immediately."""

    def __init__(self, respond: Callable[[BatchItem], Any] | Any = None, *,
                 usage: Usage = DEFAULT_USAGE):
        self.respond = respond
        self.usage = usage
        self.submitted: list[tuple[list[BatchItem], Any]] = []
        self._items_by_batch: dict[str, list[BatchItem]] = {}

    async def submit(self, items: list[BatchItem], *,
                     schema: Any = None) -> str:
        self.submitted.append((items, schema))
        batch_id = f"mock-batch-{len(self.submitted)}"
        self._items_by_batch[batch_id] = items
        return batch_id

    async def status(self, batch_id: str) -> str:
        return "ended"

    async def results(self, batch_id: str) -> list[BatchResult]:
        out: list[BatchResult] = []
        for item in self._items_by_batch[batch_id]:
            payload = self.respond(item) if callable(self.respond) \
                else self.respond
            if payload is None:
                out.append(BatchResult(custom_id=item.custom_id, ok=False,
                                       error="errored: canned failure"))
            else:
                out.append(BatchResult(
                    custom_id=item.custom_id, ok=True,
                    text=payload.model_dump_json(), usage=self.usage))
        return out
