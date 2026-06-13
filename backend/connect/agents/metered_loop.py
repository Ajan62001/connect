"""The shared metered tool-loop skeleton (design: agent-runtime-consolidation.md).

Both the investigation runner and the workspace agent drive a hand-rolled
tool loop with the SAME control flow — iterate to a cap, cooperative-cancel
check, force the terminal tool when policy says so, run one tool turn, append
the assistant message, dispatch tool calls, append the tool_result — and differ
only in POLICY: the budget/degrade ladder, how each turn is metered (which
ledger purpose, which spend user), the progress sink (job_event SSE vs a turn
callback), how an empty (no-tool) turn is handled, and the terminal tool name.

This function owns the skeleton; the policy is injected via hooks so each engine
keeps its own discipline. Extracted from two near-identical copies.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Sequence

from connect.llm.provider import (
    LLMProvider,
    ToolCall,
    ToolDef,
    ToolOutcome,
    ToolTurn,
    assistant_message,
    tool_result_message,
)
from connect.llm.tiers import ModelTier

Executor = Callable[[ToolCall], Awaitable[ToolOutcome]]


async def run_metered_tool_loop(
        provider: LLMProvider, *,
        system: str,
        tools: Sequence[ToolDef],
        executor: Executor,
        messages: list[dict[str, Any]],
        tier: ModelTier,
        max_tokens: int,
        max_iters: int,
        terminal_tool: str,
        should_force: Callable[[int], Awaitable[bool]],
        after_turn: Callable[[int, ToolTurn, bool], Awaitable[None]],
        on_empty_turn: Callable[[ToolTurn], Awaitable[bool]],
        is_concluded: Callable[[], bool],
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        ) -> int:
    """Drive the loop; return the number of turns executed. Hook contract:

    - ``should_force(n)`` — before turn ``n``, return True to force
      ``terminal_tool`` (last iteration / budget ladder). MAY raise to abort
      the whole run (the workspace agent re-raises BudgetExceeded when nothing
      has been spent yet).
    - ``after_turn(n, turn, forced)`` — meter the turn (ledger row + budget
      debit) and emit progress. Runs BEFORE the next iteration's
      ``should_force``, so budget look-ahead sees this turn's spend.
    - ``on_empty_turn(turn)`` — the model returned no tool calls; the assistant
      message is already appended. Return True to stop (the text is the
      answer), False to continue (e.g. inject a "you must finish" nudge and
      force the next turn).
    - ``is_concluded()`` — the terminal tool fired; stop before the next turn.
    - ``should_cancel()`` — cooperative cancel; a truthy return stops the loop.
      An impl may instead raise (the investigation runner raises
      CancelledError so the dossier is marked cancelled).
    """
    turns = 0
    for n in range(1, max_iters + 1):
        if is_concluded():
            break
        if should_cancel is not None and await should_cancel():
            break
        forced = await should_force(n)
        turn = await provider.complete_with_tools(
            system=system, messages=messages, tools=tools, tier=tier,
            max_tokens=max_tokens,
            tool_choice=terminal_tool if forced else None)
        turns = n
        await after_turn(n, turn, forced)
        messages.append(assistant_message(turn))
        if not turn.tool_calls:
            if await on_empty_turn(turn):
                break
            continue
        results = [(c, await executor(c)) for c in turn.tool_calls]
        messages.append(tool_result_message(results))
    return turns
