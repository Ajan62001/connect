"""StoryService — the story dossier lifecycle + the synthesis-only pipeline
(gather -> synthesize). One 'story' job per run; the registered handler
reconstructs (seed, options) from the self-describing payload and drives run().
Durable like the other dossier engines: the 'scope' and 'synthesize' sections
plus the job_event log mean a crash loses nothing and a re-run is idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.analysis.budget import AnalysisBudget
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import Governor
from connect.orchestration import events
from connect.story.factset import resolve_factset
from connect.story.schema import StoryOptions, StorySeed
from connect.story.synthesize import synthesize_story
from connect.storage.pg import Jsonb, utc_now
from connect.workers.queue import JobQueue
from connect.workers.registry import CancelToken

log = logging.getLogger(__name__)


class StoryService:
    def __init__(self, pool: AsyncConnectionPool, *, jobs: JobQueue,
                 provider: LLMProvider | None, governor: Governor,
                 embedder: Any = None, vectors: Any = None,
                 budget_usd: float = 0.40):
        self.pool = pool
        self.jobs = jobs
        self.provider = provider
        self.governor = governor
        self.embedder = embedder
        self.vectors = vectors
        self.budget_usd = budget_usd

    def default_options(self) -> StoryOptions:
        return StoryOptions()

    # -- start ----------------------------------------------------------------

    async def start(self, seed: StorySeed, options: StoryOptions | None = None,
                    *, owner_id: int,
                    visibility: str | None = None) -> tuple[int, int]:
        """Create dossier(kind='story') + enqueue the job. Raises LookupError
        when the source row is missing or not visible to the owner."""
        opts = options or self.default_options()
        async with self.pool.connection() as conn:
            subject, input_type = await self._resolve_subject(
                conn, seed, viewer=owner_id)
            async with conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO dossier (kind, input_text, input_type,"
                    " budget_usd, status, created_at, owner_id, visibility)"
                    " VALUES ('story', %s, %s, %s, 'pending', %s, %s, %s)"
                    " RETURNING id",
                    (subject, input_type, self.budget_usd, utc_now(),
                     owner_id, visibility or "shared"))
                dossier_id = int((await cur.fetchone())["id"])
        job_id = await self.jobs.enqueue(
            "story",
            {"dossier_id": dossier_id, "seed": seed.model_dump(),
             "options": opts.model_dump()},
            dossier_id=dossier_id, owner_id=owner_id)
        return dossier_id, job_id

    async def _resolve_subject(self, conn: psycopg.AsyncConnection,
                               seed: StorySeed, *,
                               viewer: int) -> tuple[str, str]:
        if seed.investigation_id is not None:
            cur = await conn.execute(
                "SELECT input_text FROM dossier WHERE id = %s AND kind ="
                " 'investigation' AND (owner_id = %s OR visibility = 'shared')",
                (seed.investigation_id, viewer))
            row = await cur.fetchone()
            if row is None:
                raise LookupError(
                    f"investigation {seed.investigation_id} not found")
            return row["input_text"], "investigation"
        if seed.story_id is not None:
            cur = await conn.execute("SELECT title FROM story WHERE id = %s",
                                     (seed.story_id,))
            row = await cur.fetchone()
            if row is None:
                raise LookupError(f"story {seed.story_id} not found")
            return (row["title"] or f"Story #{seed.story_id}"), "story"
        if seed.workspace_id is not None:
            cur = await conn.execute(
                "SELECT name FROM workspace WHERE id = %s"
                " AND (owner_id = %s OR visibility = 'shared')",
                (seed.workspace_id, viewer))
            row = await cur.fetchone()
            if row is None:
                raise LookupError(f"workspace {seed.workspace_id} not found")
            return row["name"], "workspace"
        if seed.topic:
            return seed.topic.strip(), "topic"
        raise LookupError("no story source")

    async def job_for(self, dossier_id: int) -> int | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM job WHERE dossier_id = %s"
                " ORDER BY id DESC LIMIT 1", (dossier_id,))
            row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def cancel(self, dossier_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status FROM dossier WHERE id = %s AND kind = 'story'",
                (dossier_id,))
            row = await cur.fetchone()
            if row is None or row["status"] not in ("pending", "running"):
                return False
            job_id = await self.job_for(dossier_id)
            if job_id is not None:
                await self.jobs.request_cancel(job_id)
            await self._set_dossier(conn, dossier_id, "cancelled",
                                    finished=True)
        return True

    # -- run (the job body) ---------------------------------------------------

    async def run(self, conn: psycopg.AsyncConnection, dossier_id: int,
                  seed: StorySeed, opts: StoryOptions, *, job_id: int,
                  cancel: CancelToken | None = None) -> None:
        if self.provider is None:
            await self._set_dossier(conn, dossier_id, "failed", finished=True,
                                    error="ANTHROPIC_API_KEY not set")
            raise LLMError("LLM provider not configured")

        async def emit(type_: str, data: dict[str, Any] | None = None) -> int:
            return await events.emit(conn, job_id, type_, data)

        cur = await conn.execute(
            "SELECT owner_id FROM dossier WHERE id = %s", (dossier_id,))
        owner_id = (await cur.fetchone())["owner_id"]
        await self._set_dossier(conn, dossier_id, "running", started=True)
        budget = AnalysisBudget(self.budget_usd)
        stage = "scope"
        try:
            if cancel is not None:
                await cancel.raise_if_cancelled()
            await self._section_start(conn, dossier_id, "scope")
            factset = await resolve_factset(
                conn, seed, viewer=owner_id, embedder=self.embedder,
                vectors=self.vectors)
            await self._write_section(
                conn, dossier_id, "scope",
                {**factset.as_content(), "fact_count": len(factset.facts)})
            await emit("section_completed",
                       {"section": "scope",
                        "summary": f"{len(factset.facts)} facts gathered"})

            stage = "synthesize"
            if cancel is not None:
                await cancel.raise_if_cancelled()
            await self._section_start(conn, dossier_id, "synthesize")
            result = await synthesize_story(
                conn, self.provider, factset=factset, options=opts,
                budget=budget, governor=self.governor, viewer=owner_id)
            await self._write_section(conn, dossier_id, "synthesize", result)
            await self._set_title(conn, dossier_id, result.get("title"))
            await emit("section_completed",
                       {"section": "synthesize",
                        "summary": result.get("title") or "story written"})
        except Exception as e:
            await self._section_finish(conn, dossier_id, stage, "failed")
            status = "cancelled" if _is_cancel(e) else "failed"
            await self._set_dossier(conn, dossier_id, status, finished=True,
                                    error=None if status == "cancelled"
                                    else str(e))
            await self._save_usage(conn, dossier_id, budget)
            raise
        await self._set_dossier(conn, dossier_id, "completed", finished=True)
        await self._save_usage(conn, dossier_id, budget)

    # -- dossier / section plumbing ------------------------------------------

    async def _set_title(self, conn: psycopg.AsyncConnection, dossier_id: int,
                         title: str | None) -> None:
        if not title:
            return
        async with conn.transaction():
            await conn.execute("UPDATE dossier SET title = %s WHERE id = %s",
                               (title[:300], dossier_id))

    async def _save_usage(self, conn: psycopg.AsyncConnection, dossier_id: int,
                          budget: AnalysisBudget) -> None:
        async with conn.transaction():
            await conn.execute(
                "UPDATE dossier SET model_usage = %s WHERE id = %s",
                (Jsonb({"cost_usd": round(budget.spent_usd, 6)}), dossier_id))

    async def _section_start(self, conn: psycopg.AsyncConnection,
                             dossier_id: int, stage: str) -> None:
        now = utc_now()
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at) VALUES (%s,%s,'running','{}',%s)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status='running', content='{}'::jsonb,"
                " created_at=EXCLUDED.created_at, updated_at=NULL",
                (dossier_id, stage, now))
            await conn.execute(
                "UPDATE dossier SET current_stage = %s WHERE id = %s",
                (stage, dossier_id))

    async def _write_section(self, conn: psycopg.AsyncConnection,
                             dossier_id: int, stage: str,
                             content: dict[str, Any],
                             status: str = "completed") -> None:
        now = utc_now()
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO dossier_section (dossier_id, stage, status,"
                " content, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (dossier_id, stage) DO UPDATE SET"
                " status=EXCLUDED.status, content=EXCLUDED.content,"
                " updated_at=EXCLUDED.updated_at",
                (dossier_id, stage, status, Jsonb(content), now, now))

    async def _section_finish(self, conn: psycopg.AsyncConnection,
                              dossier_id: int, stage: str,
                              status: str) -> None:
        async with conn.transaction():
            await conn.execute(
                "UPDATE dossier_section SET status = %s, updated_at = %s"
                " WHERE dossier_id = %s AND stage = %s AND status = 'running'",
                (status, utc_now(), dossier_id, stage))

    async def _set_dossier(self, conn: psycopg.AsyncConnection,
                           dossier_id: int, status: str, *,
                           started: bool = False, finished: bool = False,
                           error: str | None = None) -> None:
        sets, params = ["status = %s"], [status]
        if started:
            sets.append("started_at = %s")
            params.append(utc_now())
        if finished:
            sets.append("finished_at = %s")
            params.append(utc_now())
        if error is not None:
            sets.append("error = %s")
            params.append(error)
        params.append(dossier_id)
        async with conn.transaction():
            await conn.execute(
                f"UPDATE dossier SET {', '.join(sets)} WHERE id = %s", params)


def _is_cancel(exc: BaseException) -> bool:
    import asyncio
    return isinstance(exc, asyncio.CancelledError)
