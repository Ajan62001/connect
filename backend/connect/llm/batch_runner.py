"""Message Batches behind a small submit/status/results interface.

Anthropic's Batches API (verified against the installed SDK 0.109.1:
client.messages.batches.create/retrieve/results) processes Messages requests
at 50% price; most batches finish within an hour. The runner is deliberately
dumb — the enrichment sweep owns selection, persistence and the ledger; jobs
poll ``status`` via the existing JobRunner pattern and feed ``results``
through the SAME persistence path as sync calls.

Structured outputs inside a batch use the API's output_config.format
json_schema (the exact transform messages.parse applies), and the sweep
validates the returned JSON back into the contract — so batch and sync
results are interchangeable.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import BaseModel, TypeAdapter

from connect.llm.provider import LLMError, Usage

log = logging.getLogger(__name__)

#: processing states the sweep job polls on (mirrors the API's
#: MessageBatch.processing_status values).
BATCH_ENDED = "ended"


@dataclass(frozen=True)
class BatchItem:
    """One request in a batch: a single-user-turn structured completion."""
    custom_id: str
    model: str
    system: str | None
    user_text: str
    max_tokens: int


@dataclass(frozen=True)
class BatchResult:
    custom_id: str
    ok: bool
    text: str = ""
    error: str | None = None
    usage: Usage = field(default_factory=Usage)


class BatchRunner(ABC):
    @abstractmethod
    async def submit(self, items: list[BatchItem], *,
                     schema: type[BaseModel] | None = None) -> str:
        """Submit one Message Batch; returns the provider batch id."""

    @abstractmethod
    async def status(self, batch_id: str) -> str:
        """Provider processing status ('ended' means results are ready)."""

    @abstractmethod
    async def results(self, batch_id: str) -> list[BatchResult]:
        """All per-request results for an ended batch."""


def _output_format_param(schema: type[BaseModel]) -> dict[str, Any]:
    """The output_config.format param for ``schema`` — the same transform
    messages.parse applies, so batch and sync requests are byte-equivalent."""
    json_schema = TypeAdapter(schema).json_schema()
    try:
        # private SDK helper; fall back to the raw schema if it moves
        from anthropic.lib._parse._transform import transform_schema
        json_schema = transform_schema(json_schema)
    except Exception:  # noqa: BLE001 — raw schema still works server-side
        log.warning("anthropic transform_schema unavailable; "
                    "sending raw pydantic json schema")
    return {"type": "json_schema", "schema": json_schema}


class AnthropicBatchRunner(BatchRunner):
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def submit(self, items: list[BatchItem], *,
                     schema: type[BaseModel] | None = None) -> str:
        requests: list[dict[str, Any]] = []
        for item in items:
            params: dict[str, Any] = {
                "model": item.model,
                "max_tokens": item.max_tokens,
                "messages": [{"role": "user", "content": item.user_text}],
            }
            if item.system is not None:
                params["system"] = item.system
            if schema is not None:
                params["output_config"] = {
                    "format": _output_format_param(schema)}
            requests.append({"custom_id": item.custom_id, "params": params})
        try:
            batch = await self._client.messages.batches.create(
                requests=requests)
        except anthropic.APIError as e:
            raise LLMError(f"batch submit failed: {e}") from e
        return batch.id

    async def status(self, batch_id: str) -> str:
        try:
            batch = await self._client.messages.batches.retrieve(batch_id)
        except anthropic.APIError as e:
            raise LLMError(f"batch retrieve failed: {e}") from e
        return batch.processing_status

    async def results(self, batch_id: str) -> list[BatchResult]:
        out: list[BatchResult] = []
        try:
            async for entry in await self._client.messages.batches.results(
                    batch_id):
                out.append(self._to_result(entry))
        except anthropic.APIError as e:
            raise LLMError(f"batch results failed: {e}") from e
        return out

    @staticmethod
    def _to_result(entry: Any) -> BatchResult:
        result = entry.result
        if result.type == "succeeded":
            msg = result.message
            text = "".join(
                b.text for b in msg.content if b.type == "text")
            usage = Usage(
                input_tokens=msg.usage.input_tokens or 0,
                output_tokens=msg.usage.output_tokens or 0,
                cache_read_tokens=getattr(
                    msg.usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(
                    msg.usage, "cache_creation_input_tokens", 0) or 0,
            )
            return BatchResult(custom_id=entry.custom_id, ok=True,
                               text=text, usage=usage)
        error = getattr(result, "error", None)
        return BatchResult(
            custom_id=entry.custom_id, ok=False,
            error=f"{result.type}: {error}" if error else result.type)

    async def aclose(self) -> None:
        await self._client.close()
