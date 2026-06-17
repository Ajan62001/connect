"""ContentService — the campaign lifecycle + the generate / publish pipelines.

Generate (one 'content_generate' job per campaign): gather a CLOSED fact-set
for the subject, then per requested format run a grounded structured LLM call,
render any image cards, and insert a 'draft' content_item. Publish (one
'content_publish' job per due item): dispatch to the platform adapter and mark
the item published/failed. Durable like the dossier engines — the job wrapper
owns started/done/error; this owns the campaign + item rows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.analysis.budget import AnalysisBudget
from connect.content import publish as publish_mod
from connect.content.factset import resolve_content_factset
from connect.content.generate import FormatResult, generate_format
from connect.content.render import render_carousel
from connect.content import reel as reel_render
from connect.content.schema import ContentOptions, ContentSeed, FORMAT_SCHEMA
from connect.social import palettes
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import Governor
from connect.orchestration import events
from connect.social import settings as post_settings
from connect.social.card import render_card
from connect.storage import campaigns as campaign_dao
from connect.storage import content_items as item_dao
from connect.storage.pg import utc_now
from connect.workers.queue import JobQueue
from connect.workers.registry import CancelToken

log = logging.getLogger(__name__)


class ContentService:
    def __init__(self, pool: AsyncConnectionPool, *, jobs: JobQueue,
                 provider: LLMProvider | None, governor: Governor,
                 settings: Any, card_store: Any, logo_store: Any = None,
                 reel_store: Any = None,
                 embedder: Any = None, vectors: Any = None,
                 budget_usd: float = 0.50):
        self.pool = pool
        self.jobs = jobs
        self.provider = provider
        self.governor = governor
        self.settings = settings
        self.card_store = card_store
        self.logo_store = logo_store
        self.reel_store = reel_store
        self.embedder = embedder
        self.vectors = vectors
        self.budget_usd = budget_usd

    # -- start ----------------------------------------------------------------

    async def start(self, seed: ContentSeed, formats: list[str],
                    options: ContentOptions | None = None, *, owner_id: int,
                    visibility: str | None = None) -> tuple[int, int]:
        """Create the campaign + enqueue its generation job. Raises LookupError
        when the source row is missing or not visible to the owner."""
        opts = options or ContentOptions()
        async with self.pool.connection() as conn:
            subject, input_type = await self._resolve_subject(
                conn, seed, viewer=owner_id)
            campaign_id = await campaign_dao.insert(
                conn, owner_id=owner_id, subject=subject,
                input_type=input_type, seed=seed.model_dump(),
                formats=list(formats), options=opts.model_dump(),
                visibility=visibility or "shared")
        job_id = await self.jobs.enqueue(
            "content_generate",
            {"campaign_id": campaign_id, "seed": seed.model_dump(),
             "formats": list(formats), "options": opts.model_dump()},
            owner_id=owner_id)
        return campaign_id, job_id

    async def _resolve_subject(self, conn: psycopg.AsyncConnection,
                               seed: ContentSeed, *,
                               viewer: int) -> tuple[str, str]:
        if seed.story_dossier_id is not None:
            cur = await conn.execute(
                "SELECT title, input_text FROM dossier WHERE id = %s AND kind"
                " = 'story' AND (owner_id = %s OR visibility = 'shared')",
                (seed.story_dossier_id, viewer))
            row = await cur.fetchone()
            if row is None:
                raise LookupError(f"story {seed.story_dossier_id} not found")
            return (row["title"] or row["input_text"]
                    or f"Story #{seed.story_dossier_id}"), "story"
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
                raise LookupError(f"story thread {seed.story_id} not found")
            return (row["title"] or f"Thread #{seed.story_id}"), "thread"
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
        raise LookupError("no content source")

    async def job_for(self, campaign_id: int) -> int | None:
        async with self.pool.connection() as conn:
            return await campaign_dao.job_id_for(conn, campaign_id)

    async def cancel(self, campaign_id: int) -> bool:
        async with self.pool.connection() as conn:
            row = await conn.execute(
                "SELECT status FROM campaign WHERE id = %s", (campaign_id,))
            r = await row.fetchone()
            if r is None or r["status"] not in ("pending", "running"):
                return False
            job_id = await campaign_dao.job_id_for(conn, campaign_id)
            if job_id is not None:
                await self.jobs.request_cancel(job_id)
            await campaign_dao.set_status(conn, campaign_id, "cancelled",
                                          finished=True)
        return True

    # -- run (the content_generate job body) ----------------------------------

    async def run(self, conn: psycopg.AsyncConnection, campaign_id: int,
                  seed: ContentSeed, formats: list[str], opts: ContentOptions,
                  *, job_id: int, cancel: CancelToken | None = None) -> str:
        if self.provider is None:
            await campaign_dao.set_status(conn, campaign_id, "failed",
                                          finished=True,
                                          error="ANTHROPIC_API_KEY not set")
            raise LLMError("LLM provider not configured")

        async def emit(type_: str, data: dict[str, Any] | None = None) -> int:
            return await events.emit(conn, job_id, type_, data)

        cur = await conn.execute(
            "SELECT owner_id, visibility FROM campaign WHERE id = %s",
            (campaign_id,))
        crow = await cur.fetchone()
        if crow is None:
            # the campaign was deleted (e.g. cancelled+removed) before the job
            # got picked up — nothing to generate.
            return "campaign gone"
        owner_id, visibility = crow["owner_id"], crow["visibility"]
        await campaign_dao.set_status(conn, campaign_id, "running",
                                      started=True)
        budget = AnalysisBudget(self.budget_usd)
        eff = await post_settings.get_global(conn)
        made: list[int] = []
        try:
            if cancel is not None:
                await cancel.raise_if_cancelled()
            factset = await resolve_content_factset(
                conn, seed, viewer=owner_id, embedder=self.embedder,
                vectors=self.vectors)
            if not factset.facts:
                raise ValueError("no facts found for this subject")
            await emit("section_completed",
                       {"section": "gather",
                        "summary": f"{len(factset.facts)} facts gathered"})

            for fmt in formats:
                if cancel is not None:
                    await cancel.raise_if_cancelled()
                result = await generate_format(
                    conn, self.provider, fmt=fmt, factset=factset,
                    options=opts, settings=eff, budget=budget,
                    governor=self.governor, viewer=owner_id)
                card_shas = await self._render(result, eff, topic=seed.topic,
                                               options=opts)
                item_id = await item_dao.insert(
                    conn, campaign_id=campaign_id, owner_id=owner_id,
                    platform=result.platform, format=fmt,
                    content=result.content.model_dump(),
                    sources=[s.model_dump() for s in result.sources],
                    grounding=result.grounding, card_shas=card_shas,
                    visibility=visibility)
                made.append(item_id)
                await emit("item_generated", {"item_id": item_id,
                                              "format": fmt})
        except Exception as e:
            status = "cancelled" if _is_cancel(e) else "failed"
            await campaign_dao.set_status(
                conn, campaign_id, status, finished=True,
                error=None if status == "cancelled" else str(e))
            raise
        await campaign_dao.set_status(conn, campaign_id, "completed",
                                      finished=True)
        return f"{len(made)} items"

    def _render_settings(self, options: Any) -> Any:
        """Per-reel render settings: the global settings with the campaign's
        reel controls (voice / engine / music) overlaid. Returns the global
        settings unchanged when there are no overrides."""
        if options is None:
            return self.settings
        upd: dict[str, Any] = {}
        if getattr(options, "voice_id", None):
            upd["elevenlabs_voice_id"] = options.voice_id
        if getattr(options, "tts_engine", None):
            upd["reel_tts_engine"] = options.tts_engine
        if getattr(options, "music_volume", None) is not None:
            upd["reel_music_volume"] = options.music_volume
        if getattr(options, "music", True) is False:
            upd["reel_music_dir"] = None
        if getattr(options, "presenter", False):
            cur = getattr(self.settings, "reel_presenter", "off") or "off"
            upd["reel_presenter"] = cur if cur != "off" else "pip"
        if getattr(options, "caption_style", None):
            upd["reel_caption_style"] = options.caption_style
        if getattr(options, "video", None) is not None:
            upd["reel_use_video"] = options.video
        if not upd:
            return self.settings
        try:
            return self.settings.model_copy(update=upd)
        except Exception:  # noqa: BLE001 — fall back to global on any copy issue
            return self.settings

    async def _render(self, result: FormatResult, eff: Any, *,
                      topic: str | None = None, options: Any = None) -> list[str]:
        """Render image cards (ig_card / ig_carousel) and store them; text
        formats have no cards. Rendering is CPU-bound -> a worker thread.
        When auto-theming is on, the card palette is resolved per item from the
        campaign topic + the model's suggested palette."""
        suggested = getattr(result.content, "suggested_palette", None)
        themed, _ = palettes.resolve_post_theme(eff, topic=topic,
                                                suggested=suggested)
        logo = post_settings.load_logo(self.logo_store, themed)
        if result.fmt == "ig_card":
            images = await asyncio.to_thread(
                lambda: [render_card(result.content, settings=themed,
                                     logo=logo)])
        elif result.fmt == "ig_carousel":
            images = await asyncio.to_thread(
                render_carousel, result.content, settings=themed, logo=logo)
        elif result.fmt == "ig_reel":
            # the reel renderer (Pillow frames + ffmpeg + TTS) returns one MP4;
            # its sha rides in card_shas like a card sha. Per-reel voice/music
            # controls are applied via the effective render settings.
            mp4 = await asyncio.to_thread(
                reel_render.render_reel, result.content, settings=themed,
                logo=logo, tts_settings=self._render_settings(options))
            rel = self.reel_store.put(mp4)
            return [rel.rsplit("/", 1)[-1]]
        else:
            return []
        shas: list[str] = []
        for img in images:
            rel = self.card_store.put(img)
            shas.append(rel.rsplit("/", 1)[-1])
        return shas

    # -- publish (the content_publish job body) -------------------------------

    async def publish_item(self, conn: psycopg.AsyncConnection,
                           item_id: int, *, target: str = "direct") -> str:
        """Publish one item. ``target`` is 'direct' (platform adapter) or
        'zapier' (POST a payload to the configured Zapier webhook)."""
        row = await item_dao.get_for_publish(conn, item_id)
        if row is None:
            return "gone"
        if row["status"] not in ("scheduled", "approved"):
            return "already handled"
        fmt = row["format"]
        content = dict(row["content"] or {})
        card_shas = list(row["card_shas"] or [])
        s = self.settings

        card_urls: list[str] = []
        if fmt in ("ig_card", "ig_carousel", "ig_reel"):
            base = getattr(s, "public_base_url", None)
            if not base:
                asset = "reel video" if fmt == "ig_reel" else "card image"
                dest = "Zapier" if target == "zapier" else "Instagram"
                await item_dao.mark_failed(
                    conn, item_id,
                    error=f"CONNECT_PUBLIC_BASE_URL not set — {dest} cannot"
                          f" fetch the {asset}")
                return "failed"
            if fmt == "ig_reel":
                card_urls = [f"{base.rstrip('/')}/api/social/reel/{sha}.mp4"
                             for sha in card_shas]
            else:
                card_urls = [f"{base.rstrip('/')}/api/social/card/{sha}.jpg"
                             for sha in card_shas]

        try:
            if target == "zapier":
                ref = await publish_mod.publish_via_zapier(
                    s, fmt=fmt, content=content, card_urls=card_urls,
                    item_id=item_id)
            else:
                ref = await publish_mod.publish_item(
                    s, fmt=fmt, content=content, card_urls=card_urls)
        except Exception as e:  # noqa: BLE001 — publish failure is item data
            log.info("publish item %s (%s) failed: %s", item_id, target, e)
            await item_dao.mark_failed(conn, item_id, error=str(e))
            return "failed"
        await item_dao.mark_published(conn, item_id, publish_ref=ref)
        return "published"

    # -- re-render (the content_render job body) ------------------------------

    async def rerender(self, conn: psycopg.AsyncConnection, item_id: int,
                       options: ContentOptions, *, job_id: int,
                       owner_id: int) -> str:
        """Re-render an item's media from its CURRENT (possibly hand-edited)
        content with the given reel controls (voice / music). The edited script
        is rendered as-is — it is the owner's authored copy. Text formats have
        no media; image/video formats get fresh card_shas."""
        item = await item_dao.get(conn, item_id, viewer=owner_id)
        if item is None:
            return "gone"
        if item.format not in ("ig_card", "ig_carousel", "ig_reel"):
            return "no media"
        content_obj = FORMAT_SCHEMA[item.format](**item.content)
        result = FormatResult(fmt=item.format, platform=item.platform,
                              content=content_obj, sources=[],
                              grounding=item.grounding or {})
        eff = await post_settings.get_global(conn)
        card_shas = await self._render(result, eff, topic=None, options=options)
        await item_dao.set_card_shas(conn, item_id, owner_id=owner_id,
                                     card_shas=card_shas)
        return f"rendered {len(card_shas)}"


def _is_cancel(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.CancelledError)


# utc_now re-export kept for symmetry with the other services (unused here but
# imported by tests that build rows directly).
__all__ = ["ContentService", "utc_now"]
