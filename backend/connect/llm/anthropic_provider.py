"""Anthropic implementation of LLMProvider.

Built against the INSTALLED SDK (anthropic 0.109.1, verified):
- structured outputs via ``client.messages.parse(..., output_format=Model)``
  — the SDK transforms the Pydantic schema into the API's
  ``output_config.format`` json_schema and validates the response back into
  the model (``response.parsed_output``). One retry on validation failure,
  then LLMError; the choice of mechanism is invisible to callers.
- usage comes back on every response (input/output/cache_read tokens) and is
  passed to the spend ledger by callers via the returned Usage.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

import anthropic
import pydantic

from connect.llm.provider import (
    Completion,
    LLMError,
    LLMProvider,
    StructuredCompletion,
    TModel,
    ToolCall,
    ToolDef,
    ToolTurn,
    Usage,
)
from connect.llm.tiers import DEFAULT_TIER_MODELS, ModelTier

log = logging.getLogger(__name__)


def _usage(resp_usage: Any) -> Usage:
    return Usage(
        input_tokens=getattr(resp_usage, "input_tokens", 0) or 0,
        output_tokens=getattr(resp_usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(
            resp_usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(
            resp_usage, "cache_creation_input_tokens", 0) or 0,
    )


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, *,
                 tier_models: Mapping[ModelTier, str] | None = None,
                 max_retries: int = 2):
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=max_retries)
        self._tier_models = dict(tier_models or DEFAULT_TIER_MODELS)

    def model_for(self, tier: ModelTier) -> str:
        return self._tier_models[tier]

    async def complete(self, *, system: str | None,
                       messages: Sequence[dict[str, Any]],
                       tier: ModelTier, max_tokens: int) -> Completion:
        model = self.model_for(tier)
        kwargs: dict[str, Any] = {}
        if system is not None:
            kwargs["system"] = system
        try:
            resp = await self._client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=list(messages), **kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"anthropic call failed: {e}") from e
        text = "".join(
            block.text for block in resp.content if block.type == "text")
        return Completion(text=text, model=resp.model, usage=_usage(resp.usage))

    async def complete_with_tools(self, *, system: str | None,
                                  messages: Sequence[dict[str, Any]],
                                  tools: Sequence[ToolDef],
                                  tier: ModelTier, max_tokens: int,
                                  cache: bool = True,
                                  tool_choice: str | None = None,
                                  ) -> ToolTurn:
        """One tools-enabled messages.create turn (SDK 0.109.1 surface:
        tools=[{name, description, input_schema}], tool_choice
        {'type':'tool','name':...}, content blocks 'text'/'tool_use').
        ``cache`` marks the system block ``cache_control: ephemeral`` — the
        prompt prefix (tools + system) is cached, so every later iteration
        re-reads it at ~0.1x the input rate."""
        model = self.model_for(tier)
        kwargs: dict[str, Any] = {}
        if system is not None:
            if cache:
                kwargs["system"] = [{
                    "type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}]
            else:
                kwargs["system"] = system
        if tool_choice is not None:
            kwargs["tool_choice"] = {"type": "tool", "name": tool_choice}
        try:
            resp = await self._client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=list(messages),
                tools=[t.model_dump() for t in tools], **kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"anthropic tool call failed: {e}") from e
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                raw.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name,
                                      input=dict(block.input or {})))
                raw.append({"type": "tool_use", "id": block.id,
                            "name": block.name,
                            "input": dict(block.input or {})})
            else:  # pragma: no cover — future block types replay as-is
                raw.append(block.model_dump(mode="json", exclude_none=True))
        return ToolTurn(
            text="".join(text_parts), tool_calls=calls,
            stop_reason=resp.stop_reason or "end_turn", raw_content=raw,
            model=resp.model, usage=_usage(resp.usage))

    async def complete_structured(self, *, system: str | None,
                                  messages: Sequence[dict[str, Any]],
                                  schema: type[TModel],
                                  tier: ModelTier, max_tokens: int,
                                  ) -> StructuredCompletion[TModel]:
        model = self.model_for(tier)
        kwargs: dict[str, Any] = {}
        if system is not None:
            kwargs["system"] = system

        last_error: Exception | None = None
        usage = Usage()
        for attempt in range(2):  # one retry on a schema-shaped failure
            try:
                resp = await self._client.messages.parse(
                    model=model, max_tokens=max_tokens,
                    messages=list(messages), output_format=schema, **kwargs)
            except anthropic.APIError as e:
                raise LLMError(f"anthropic call failed: {e}") from e
            except (pydantic.ValidationError, ValueError) as e:
                # response came back but did not validate — retry once
                last_error = e
                log.warning("structured parse failed (attempt %s): %s",
                            attempt + 1, e)
                continue
            usage = Usage(
                input_tokens=usage.input_tokens + _usage(resp.usage).input_tokens,
                output_tokens=usage.output_tokens + _usage(resp.usage).output_tokens,
                cache_read_tokens=usage.cache_read_tokens
                + _usage(resp.usage).cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens
                + _usage(resp.usage).cache_creation_tokens,
            )
            if resp.parsed_output is None:
                last_error = LLMError(
                    f"no parsed output (stop_reason={resp.stop_reason!r})")
                log.warning("structured parse returned no output "
                            "(attempt %s): %s", attempt + 1, last_error)
                continue
            return StructuredCompletion[schema](  # type: ignore[valid-type]
                output=resp.parsed_output, model=resp.model, usage=usage)
        raise LLMError(
            f"structured output failed after retry: {last_error}")

    async def aclose(self) -> None:
        await self._client.close()
