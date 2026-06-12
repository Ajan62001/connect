"""'brief_generate' — pre-generate today's brief for ONE user (beat
enqueues one job per 7-day-active user, staggered over an hour;
get_or_generate is idempotent — a brief already generated for today is
simply returned). Jobs without a user_id (pre-tenancy leftovers) no-op."""

from __future__ import annotations

from typing import Any

from connect.knowledge import briefing
from connect.workers.registry import WorkerContext, register


@register("brief_generate")
async def brief_generate(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    user_id = payload.get("user_id")
    if not isinstance(user_id, int):
        return "no user_id — skipped"
    response = await briefing.get_or_generate(ctx.conn, user_id)
    if response is None:  # defensive: today's brief never returns None
        return "no brief"
    return f"brief {response.brief.brief_date} user {user_id}"
