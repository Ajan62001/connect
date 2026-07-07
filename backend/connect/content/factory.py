"""ReelFactoryService — the autonomous production line: feed -> reels.

One 'reel_factory' job per run. The run itself is cheap and fast — it scouts
the hottest subjects from the corpus (content/scout.py), commissions the K
most reel-worthy, and starts one ordinary reel-led CAMPAIGN per pick. All the
heavy machinery (grounded factset, script generation, the editor loop, the
integrity gate, the ffmpeg render) then runs in the campaigns' own
content_generate jobs, in parallel, with all the existing review-queue,
SSE-progress and provenance semantics. The factory adds selection, not a
second pipeline.

The job row + its job_event log ARE the run record (no new table): the run
emits 'scouted' / 'assignment' / 'campaign_created' events, so the API can
list past runs and link to what they produced.

Runs start on demand (POST /api/factory/reels) or on the beat's daily
schedule when reel_factory_enabled is set (workers/beat.py picks the first
enabled admin as the owner).
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.analysis.budget import AnalysisBudget
from connect.content import scout
from connect.content.schema import ContentOptions, ContentSeed
from connect.content.service import ContentService
from connect.llm.provider import LLMProvider
from connect.orchestration import events
from connect.storage import workspaces as workspace_dao
from connect.workers.queue import JobQueue
from connect.workers.registry import CancelToken

log = logging.getLogger(__name__)

MAX_RUN_COUNT = 8            # reels a single run may commission
SCOUT_BUDGET_USD = 0.10      # the run's own LLM spend (picker only)


class ReelFactoryService:
    def __init__(self, pool: AsyncConnectionPool, *, jobs: JobQueue,
                 provider: LLMProvider | None, governor: Any, settings: Any,
                 content: ContentService):
        self.pool = pool
        self.jobs = jobs
        self.provider = provider
        self.governor = governor
        self.settings = settings
        self.content = content

    # -- start ------------------------------------------------------------

    async def start(self, *, owner_id: int, count: int | None = None,
                    window_hours: int | None = None,
                    options: ContentOptions | None = None,
                    visibility: str | None = None,
                    workspace_id: int | None = None) -> int:
        """Enqueue one factory run; returns the job id (the run's handle).
        ``workspace_id`` scopes the scout to that workspace's focus and binds
        the commissioned campaigns to its channel."""
        s = self.settings
        payload = {
            "count": max(1, min(int(count or s.reel_factory_count),
                                MAX_RUN_COUNT)),
            "window_hours": max(1, int(window_hours
                                       or s.reel_factory_window_hours)),
            "options": (options or ContentOptions()).model_dump(),
            "visibility": visibility or "shared",
            "owner_id": owner_id,   # handlers reconstruct from the payload
            "workspace_id": workspace_id,
        }
        return await self.jobs.enqueue("reel_factory", payload,
                                       owner_id=owner_id)

    # -- run (the reel_factory job body) -----------------------------------

    async def run(self, conn: psycopg.AsyncConnection,
                  payload: dict[str, Any], *, job_id: int, owner_id: int,
                  cancel: CancelToken | None = None) -> str:
        count = max(1, min(int(payload.get("count") or 1), MAX_RUN_COUNT))
        window_hours = max(1, int(payload.get("window_hours") or 24))
        opts = ContentOptions(**(payload.get("options") or {}))
        visibility = str(payload.get("visibility") or "shared")
        workspace_id = payload.get("workspace_id")

        async def emit(type_: str, data: dict[str, Any] | None = None) -> int:
            return await events.emit(conn, job_id, type_, data)

        # per-workspace mode: scope the scout to the workspace focus and bind
        # commissioned campaigns to its channel (voice/character/preset)
        workspace = None
        if workspace_id is not None:
            workspace = await workspace_dao.get(conn, int(workspace_id),
                                                viewer=owner_id)
            if workspace is None:
                return f"workspace {workspace_id} not found"

        candidates = await scout.scout_candidates(
            conn, window_hours=window_hours,
            dedup_days=int(getattr(self.settings,
                                   "reel_factory_dedup_days", 3)),
            workspace=workspace,
        )
        await emit("scouted", {
            "candidates": len(candidates), "window_hours": window_hours,
            "slate": [{"subject": c.subject, "kind": c.kind, "heat": c.heat,
                       "doc_count": c.doc_count, "signal": c.signal}
                      for c in candidates]})
        if not candidates:
            return "no candidates in window"

        if cancel is not None:
            await cancel.raise_if_cancelled()
        assignments = await scout.pick_assignments(
            conn, self.provider, candidates=candidates, count=count,
            window_hours=window_hours, budget=AnalysisBudget(SCOUT_BUDGET_USD),
            governor=self.governor, viewer=owner_id)

        made = 0
        for a in assignments:
            if cancel is not None:
                await cancel.raise_if_cancelled()
            c = a.candidate
            await emit("assignment", {
                "subject": c.subject, "kind": c.kind, "heat": c.heat,
                "angle": a.angle, "reason": a.reason})
            # a thread pick keeps its event-ordered facts (story_id seed);
            # events/topics resolve via hybrid retrieval over the corpus.
            if c.kind == "thread" and c.story_id is not None:
                seed = ContentSeed(story_id=c.story_id)
            else:
                seed = ContentSeed(topic=c.subject[:400])
            angle_style = (f"Lead with this angle: {a.angle}"
                           if a.angle else None)
            run_opts = opts if angle_style is None else opts.model_copy(
                update={"style": (angle_style[:200])})
            try:
                campaign_id, cjob_id = await self.content.start(
                    seed, ["ig_reel"], run_opts, owner_id=owner_id,
                    visibility=visibility,
                    channel_workspace_id=workspace_id)
            except LookupError as e:
                # a subject can vanish between scout and start (e.g. the
                # thread was merged away) — skip it, never fail the run
                log.info("factory: skipping %r (%s)", c.subject, e)
                await emit("assignment_skipped",
                           {"subject": c.subject, "error": str(e)})
                continue
            made += 1
            await emit("campaign_created", {
                "campaign_id": campaign_id, "job_id": cjob_id,
                "subject": c.subject})
        return f"{made} campaign(s) from {len(candidates)} candidate(s)"

    # -- read (the /api/factory/runs surface) ------------------------------

    async def list_runs(self, conn: psycopg.AsyncConnection, *, viewer: int,
                        limit: int = 20) -> list[dict[str, Any]]:
        """Recent factory runs, newest first — the job row plus what the run
        reported through its events. Shared runs are a common production
        surface; a PRIVATE run (its campaigns are hidden from other users)
        is visible only to its owner — its events carry the subjects and
        campaign ids the campaign API would refuse to show."""
        cur = await conn.execute(
            "SELECT id, status, error, created_at, finished_at FROM job"
            " WHERE kind = 'reel_factory'"
            " AND (owner_id = %s OR COALESCE(payload->>'visibility',"
            " 'shared') <> 'private')"
            " ORDER BY id DESC LIMIT %s",
            (viewer, limit))
        jobs = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for j in jobs:
            cur = await conn.execute(
                "SELECT type, data FROM job_event WHERE job_id = %s"
                " ORDER BY seq", (j["id"],))
            events_rows = await cur.fetchall()
            candidates, result = 0, None
            assignments: list[dict[str, Any]] = []
            campaigns: list[dict[str, Any]] = []
            for ev in events_rows:
                data = ev["data"] or {}
                if ev["type"] == "scouted":
                    candidates = int(data.get("candidates") or 0)
                elif ev["type"] == "assignment":
                    assignments.append(data)
                elif ev["type"] == "campaign_created":
                    campaigns.append(data)
                elif ev["type"] == "done":
                    result = data.get("result")
            out.append({
                "job_id": int(j["id"]), "status": j["status"],
                "created_at": j["created_at"],
                "finished_at": j["finished_at"], "error": j["error"],
                "result": result, "candidates": candidates,
                "assignments": assignments, "campaigns": campaigns})
        return out
