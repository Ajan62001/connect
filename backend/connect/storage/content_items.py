"""Content-item DAO — the review-queue units a campaign produces.

Reads are viewer-scoped (owner OR shared); review transitions (approve /
reject / schedule / edit) are owner-only and guarded by the current status so
a published item can never silently revert. ``list_due`` + ``mark_*`` serve
the beat publish path (system context — no viewer).
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.content.schema import ContentItemDetail, ContentItemPage
from connect.story.schema import StorySource
from connect.storage.pg import Jsonb, utc_now

_VISIBLE = "(owner_id = %s OR visibility = 'shared')"

_COLS = ("id, campaign_id, platform, format, status, content, sources,"
         " grounding, card_shas, visibility, owner_id, edited, scheduled_at,"
         " published_at, publish_ref, error, created_at, updated_at")


def _to_detail(row: Mapping[str, Any]) -> ContentItemDetail:
    sources = [StorySource(**s) for s in (row["sources"] or [])
               if isinstance(s, dict)]
    return ContentItemDetail(
        id=row["id"], campaign_id=row["campaign_id"], platform=row["platform"],
        format=row["format"], status=row["status"],
        content=dict(row["content"] or {}), sources=sources,
        grounding=dict(row["grounding"] or {}),
        card_shas=list(row["card_shas"] or []), visibility=row["visibility"],
        owner_id=row["owner_id"], edited=bool(row["edited"]),
        scheduled_at=_s(row["scheduled_at"]), published_at=_s(row["published_at"]),
        publish_ref=dict(row["publish_ref"]) if isinstance(
            row["publish_ref"], dict) else None,
        error=row["error"], created_at=str(row["created_at"]),
        updated_at=_s(row["updated_at"]))


def _s(v: Any) -> str | None:
    return str(v) if v else None


async def insert(conn: psycopg.AsyncConnection, *, campaign_id: int,
                 owner_id: int, platform: str, format: str,
                 content: dict[str, Any], sources: list[dict[str, Any]],
                 grounding: dict[str, Any], card_shas: list[str],
                 visibility: str) -> int:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO content_item (campaign_id, owner_id, platform,"
            " format, status, content, sources, grounding, card_shas,"
            " visibility, created_at) VALUES"
            " (%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s) RETURNING id",
            (campaign_id, owner_id, platform, format, Jsonb(content),
             Jsonb(sources), Jsonb(grounding), Jsonb(card_shas), visibility,
             utc_now()))
        return int((await cur.fetchone())["id"])


async def get(conn: psycopg.AsyncConnection, item_id: int, *,
              viewer: int) -> ContentItemDetail | None:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM content_item WHERE id = %s AND {_VISIBLE}",
        (item_id, viewer))
    row = await cur.fetchone()
    return _to_detail(row) if row else None


async def list_for_campaign(conn: psycopg.AsyncConnection, campaign_id: int, *,
                            viewer: int) -> list[ContentItemDetail]:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM content_item WHERE campaign_id = %s"
        f" AND {_VISIBLE} ORDER BY id", (campaign_id, viewer))
    return [_to_detail(r) for r in await cur.fetchall()]


async def list_queue(conn: psycopg.AsyncConnection, *, viewer: int,
                     status: str | None = None, platform: str | None = None,
                     campaign_id: int | None = None,
                     limit: int = 50, offset: int = 0) -> ContentItemPage:
    where = [_VISIBLE]
    params: list[Any] = [viewer]
    if status:
        where.append("status = %s")
        params.append(status)
    if platform:
        where.append("platform = %s")
        params.append(platform)
    if campaign_id is not None:
        where.append("campaign_id = %s")
        params.append(campaign_id)
    clause = " AND ".join(where)
    cur = await conn.execute(
        f"SELECT count(*) AS n FROM content_item WHERE {clause}", params)
    total = int((await cur.fetchone())["n"])
    cur = await conn.execute(
        f"SELECT {_COLS} FROM content_item WHERE {clause}"
        " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
        [*params, limit, offset])
    return ContentItemPage(
        items=[_to_detail(r) for r in await cur.fetchall()], total=total)


# -- review transitions (owner-only, status-guarded) ---------------------------


async def update_content(conn: psycopg.AsyncConnection, item_id: int, *,
                         owner_id: int, content: dict[str, Any]) -> bool:
    """Hand-edit the payload (owner-only; not allowed once published)."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE content_item SET content = %s, edited = true,"
            " updated_at = %s WHERE id = %s AND owner_id = %s"
            " AND status <> 'published'",
            (Jsonb(content), utc_now(), item_id, owner_id))
    return cur.rowcount > 0


async def set_card_shas(conn: psycopg.AsyncConnection, item_id: int, *,
                        owner_id: int, card_shas: list[str]) -> bool:
    """Replace the rendered assets (after a re-render); owner-only, not once
    published."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE content_item SET card_shas = %s, updated_at = %s"
            " WHERE id = %s AND owner_id = %s AND status <> 'published'",
            (Jsonb(card_shas), utc_now(), item_id, owner_id))
    return cur.rowcount > 0


async def set_status(conn: psycopg.AsyncConnection, item_id: int, *,
                     owner_id: int, status: str, from_statuses: tuple[str, ...],
                     scheduled_at: str | None = None) -> bool:
    sets = ["status = %s", "updated_at = %s"]
    params: list[Any] = [status, utc_now()]
    if scheduled_at is not None:
        sets.append("scheduled_at = %s::timestamptz")
        params.append(scheduled_at)
    placeholders = ",".join(["%s"] * len(from_statuses))
    params += [item_id, owner_id, *from_statuses]
    async with conn.transaction():
        cur = await conn.execute(
            f"UPDATE content_item SET {', '.join(sets)} WHERE id = %s"
            f" AND owner_id = %s AND status IN ({placeholders})", params)
    return cur.rowcount > 0


# -- publish path (system context: beat + content_publish handler) -------------


async def list_due(conn: psycopg.AsyncConnection) -> list[int]:
    """Ids of scheduled items whose time has come (beat due-scan)."""
    cur = await conn.execute(
        "SELECT id FROM content_item WHERE status = 'scheduled'"
        " AND scheduled_at IS NOT NULL AND scheduled_at <= now()"
        " ORDER BY scheduled_at")
    return [int(r["id"]) for r in await cur.fetchall()]


async def get_for_publish(conn: psycopg.AsyncConnection,
                          item_id: int) -> Mapping[str, Any] | None:
    """The full row for publishing — no viewer scope (the owning campaign's
    job already authorized this), but only items still awaiting publish."""
    cur = await conn.execute(
        f"SELECT {_COLS} FROM content_item WHERE id = %s", (item_id,))
    return await cur.fetchone()


async def mark_published(conn: psycopg.AsyncConnection, item_id: int, *,
                         publish_ref: dict[str, Any]) -> bool:
    """scheduled/approved -> published (idempotent: a second publish is a
    no-op because the row is no longer in a publishable state)."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE content_item SET status = 'published', publish_ref = %s,"
            " published_at = %s, error = NULL, updated_at = %s WHERE id = %s"
            " AND status IN ('scheduled','approved')",
            (Jsonb(publish_ref), utc_now(), utc_now(), item_id))
    return cur.rowcount > 0


async def mark_failed(conn: psycopg.AsyncConnection, item_id: int, *,
                      error: str) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE content_item SET status = 'failed', error = %s,"
            " updated_at = %s WHERE id = %s AND status IN"
            " ('scheduled','approved')", (error[:1000], utc_now(), item_id))
