"""'brief_generate' — pre-generate today's brief (beat enqueues one per
7-day-active user, staggered over an hour). The payload's user_id is
carried for the auth workstream; until it lands, briefs are single-user
and generation ignores it (get_or_generate is idempotent — a brief already
generated for today is simply returned)."""

from __future__ import annotations

from typing import Any

from connect.knowledge import briefing
from connect.workers.registry import WorkerContext, register


@register("brief_generate")
async def brief_generate(ctx: WorkerContext, payload: dict[str, Any]) -> str:
    response = await briefing.get_or_generate(ctx.conn)
    if response is None:  # defensive: today's brief never returns None
        return "no brief"
    return f"brief {response.brief.brief_date}"
