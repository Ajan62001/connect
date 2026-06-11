"""LLMProvider ABC + the small value objects every caller codes against.

Callers (enrichment, analysis, investigations) never import the anthropic
SDK — they get text or a validated Pydantic instance plus token usage, and
the provider hides how structured output is obtained.

v8 adds the tool-use surface: ``complete_with_tools`` is the abstract
transport primitive (one tool-enabled turn), and ``tool_loop`` is a CONCRETE
template on the ABC — the generic call-execute-feed-back loop every agentic
caller shares. The investigation runner drives its own manual loop (per-turn
ledger + budget metering + SSE) directly on ``complete_with_tools``.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Generic, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict

from connect.llm.tiers import ModelTier

TModel = TypeVar("TModel", bound=BaseModel)


class LLMError(Exception):
    """Provider-level failure (refusal, truncation, schema mismatch after
    retry, transport error). Callers treat it as 'this document failed'."""


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class Completion(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    usage: Usage


class StructuredCompletion(BaseModel, Generic[TModel]):
    """A validated instance of the requested schema + what it cost."""
    model_config = ConfigDict(frozen=True)

    output: TModel
    model: str
    usage: Usage


# -- tool use (v8) -----------------------------------------------------------------


class ToolDef(BaseModel):
    """One tool the model may call (name + description + JSON Schema)."""
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    """One tool_use block the model emitted."""
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    input: dict[str, Any]


class ToolTurn(BaseModel):
    """One assistant turn from a tool-enabled call. ``raw_content`` is the
    provider-shaped content-block list, replayable verbatim as the next
    assistant message so transcripts stay cache-exact."""
    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: list[ToolCall] = []
    stop_reason: str = "end_turn"
    raw_content: list[dict[str, Any]] = []
    model: str
    usage: Usage


class ToolOutcome(BaseModel):
    """What executing one ToolCall produced — fed back as a tool_result
    block (is_error=True surfaces validator rejections to the model)."""
    model_config = ConfigDict(frozen=True)

    content: str
    is_error: bool = False


class ToolLoopResult(BaseModel):
    """Transcript + accounting from one ``tool_loop`` run."""
    model_config = ConfigDict(frozen=True)

    turns: list[ToolTurn] = []
    messages: list[dict[str, Any]] = []
    stop: str = "max_iterations"   # 'terminal' | 'end_turn' | 'max_iterations'
    usage: Usage = Usage()


def add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens
        + b.cache_creation_tokens)


def assistant_message(turn: ToolTurn) -> dict[str, Any]:
    """The assistant message for a turn — raw_content verbatim when present,
    reconstructed from text + tool_calls otherwise (test doubles)."""
    if turn.raw_content:
        return {"role": "assistant", "content": list(turn.raw_content)}
    blocks: list[dict[str, Any]] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for call in turn.tool_calls:
        blocks.append({"type": "tool_use", "id": call.id,
                       "name": call.name, "input": call.input})
    if not blocks:
        blocks.append({"type": "text", "text": "(no content)"})
    return {"role": "assistant", "content": blocks}


def tool_result_message(results: Sequence[tuple[ToolCall, ToolOutcome]],
                        ) -> dict[str, Any]:
    """The user message carrying one turn's tool_result blocks (parallel
    calls within a turn all answer in this single message)."""
    blocks: list[dict[str, Any]] = []
    for call, outcome in results:
        block: dict[str, Any] = {
            "type": "tool_result", "tool_use_id": call.id,
            "content": outcome.content}
        if outcome.is_error:
            block["is_error"] = True
        blocks.append(block)
    return {"role": "user", "content": blocks}


ToolExecutorFn = Callable[[ToolCall], Awaitable[ToolOutcome]]


class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, *, system: str | None,
                       messages: Sequence[dict[str, Any]],
                       tier: ModelTier,
                       max_tokens: int) -> Completion:
        """One free-text completion."""

    @abstractmethod
    async def complete_structured(self, *, system: str | None,
                                  messages: Sequence[dict[str, Any]],
                                  schema: type[TModel],
                                  tier: ModelTier,
                                  max_tokens: int,
                                  ) -> StructuredCompletion[TModel]:
        """One completion constrained to ``schema``; raises LLMError when a
        valid instance cannot be obtained."""

    @abstractmethod
    async def complete_with_tools(self, *, system: str | None,
                                  messages: Sequence[dict[str, Any]],
                                  tools: Sequence[ToolDef],
                                  tier: ModelTier,
                                  max_tokens: int,
                                  cache: bool = True,
                                  tool_choice: str | None = None,
                                  ) -> ToolTurn:
        """One tool-enabled turn. ``cache`` puts a cache breakpoint on the
        system block (tools + system prefix re-read at ~0.1x each
        iteration); ``tool_choice`` forces the named tool (the runner's
        forced-conclude path)."""

    async def tool_loop(self, *, system: str | None,
                        messages: Sequence[dict[str, Any]],
                        tools: Sequence[ToolDef],
                        executor: ToolExecutorFn,
                        tier: ModelTier,
                        max_iterations: int,
                        max_tokens: int = 2048,
                        on_turn: Callable[[ToolTurn], Any] | None = None,
                        terminal_tools: frozenset[str] = frozenset(
                            {"conclude"}),
                        ) -> ToolLoopResult:
        """The generic call -> execute -> feed-back loop (concrete template).

        Stops on a successfully executed terminal tool, on an assistant
        turn with no tool calls, or after ``max_iterations`` turns. Callers
        needing per-turn metering/SSE (the investigation runner) drive
        ``complete_with_tools`` manually instead.
        """
        msgs: list[dict[str, Any]] = [dict(m) for m in messages]
        turns: list[ToolTurn] = []
        total = Usage()
        stop = "max_iterations"
        for _ in range(max_iterations):
            turn = await self.complete_with_tools(
                system=system, messages=msgs, tools=tools, tier=tier,
                max_tokens=max_tokens)
            turns.append(turn)
            total = add_usage(total, turn.usage)
            if on_turn is not None:
                maybe = on_turn(turn)
                if inspect.isawaitable(maybe):
                    await maybe
            if not turn.tool_calls:
                stop = "end_turn"
                break
            msgs.append(assistant_message(turn))
            results: list[tuple[ToolCall, ToolOutcome]] = []
            hit_terminal = False
            for call in turn.tool_calls:
                outcome = await executor(call)
                results.append((call, outcome))
                if call.name in terminal_tools and not outcome.is_error:
                    hit_terminal = True
            msgs.append(tool_result_message(results))
            if hit_terminal:
                stop = "terminal"
                break
        return ToolLoopResult(turns=turns, messages=msgs, stop=stop,
                              usage=total)

    def model_for(self, tier: ModelTier) -> str:
        """The model id this provider routes the tier to (for cost
        projection before a call is made)."""
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        """Release transport resources (default: nothing to do)."""
        return None
