"""'workspace_task' — a workspace-agent "deep" run executed asynchronously as
a background job. It runs the SAME agent as the in-request chat (mode='deep':
a higher iteration/spend ceiling), streams progress as job_event 'iteration'
rows (the shared SSE contract), and appends the assistant answer to the chat
transcript on completion.

There is NO separate task row: the job's own terminal state (done/error/
cancelled, written by the queue's execute_job + reclaimed by the beat orphan
sweep) is the single source of truth — a worker crash can no longer strand a
status, and cancellation is the standard job request_cancel path."""

from __future__ import annotations

from typing import Any

from connect.agents.workspace_agent import run_workspace_agent
from connect.orchestration import events
from connect.social import settings as post_settings
from connect.storage import characters as character_dao
from connect.storage import workspace_channels as channel_dao
from connect.storage import workspace_chats as chat_dao
from connect.storage import workspaces as workspace_dao
from connect.workers.registry import WorkerContext, register


@register("workspace_task")
async def run_workspace_task(ctx: WorkerContext,
                             payload: dict[str, Any]) -> str:
    services = ctx.services
    chat_id = int(payload["chat_id"])
    workspace_id = int(payload["workspace_id"])
    owner_id = int(payload["owner_id"])

    if services.llm is None:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    ws = await workspace_dao.get(ctx.conn, workspace_id, viewer=owner_id)
    if ws is None:
        raise LookupError(f"workspace {workspace_id} not found")
    chat = await chat_dao.get(ctx.conn, chat_id, workspace_id=workspace_id,
                              owner_id=owner_id)
    if chat is None:
        raise LookupError(f"chat {chat_id} not found")

    wire = list(chat["messages"] or [])
    transcript = list(chat["transcript"] or [])
    settings = await post_settings.effective(ctx.conn, ws)
    channel = await channel_dao.get_raw(ctx.conn, workspace_id)
    char_id = channel.get("default_character_id") if channel else None
    character = (await character_dao.get(ctx.conn, char_id)
                 if char_id else None)

    n = 0

    async def on_turn(tools: list[str]) -> None:
        nonlocal n
        n += 1
        await events.emit(ctx.conn, ctx.job_id, "iteration",
                          {"n": n, "tools": tools})

    # Let BudgetExceeded/LLMError/CancelledError propagate: the queue's
    # execute_job writes the terminal job_event (error/cancelled) and marks
    # the job. run_workspace_agent only raises BudgetExceeded when the FIRST
    # turn is refused (nothing spent); a mid-run budget hit returns a partial
    # answer, which we still record below.
    result = await run_workspace_agent(
        ctx.conn, llm=services.llm, governor=services.governor,
        embedder=services.embedder, vectors=services.vectors,
        workspace=ws, viewer=owner_id, messages=wire,
        card_store=services.card_store, logo_store=services.logo_store,
        post_settings=settings, character=character,
        on_turn=on_turn, should_cancel=ctx.cancel.cancelled, mode="deep")

    transcript.append(chat_dao.turn_dict(
        "assistant", result.final_answer or "(no answer)",
        tools_used=result.tools_used, finding=result.finding))
    await chat_dao.update(ctx.conn, chat_id, owner_id=owner_id,
                          messages=result.messages, transcript=transcript)
    return f"ok: {result.turns_completed} turns"
