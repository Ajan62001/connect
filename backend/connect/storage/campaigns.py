"""Campaign DAO — a content-pipeline generation run + its review-queue items.

Owned + shared/private exactly like dossiers (owner OR shared; a private
campaign another user cannot see reads as absent -> 404 at the router). The
generation job is found by its payload (``campaign_id``), mirroring how the
story SSE endpoint resolves a job.
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.content.schema import (
    CampaignDetail,
    CampaignListItem,
    CampaignPage,
)
from connect.storage import content_items as item_dao
from connect.storage.pg import Jsonb, utc_now

_VISIBLE = "(owner_id = %s OR visibility = 'shared')"


async def insert(conn: psycopg.AsyncConnection, *, owner_id: int,
                 subject: str, input_type: str, seed: dict[str, Any],
                 formats: list[str], options: dict[str, Any],
                 visibility: str) -> int:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO campaign (owner_id, subject, input_type, seed,"
            " formats, options, status, visibility, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s) RETURNING id",
            (owner_id, subject[:400], input_type, Jsonb(seed),
             Jsonb(formats), Jsonb(options), visibility, utc_now()))
        return int((await cur.fetchone())["id"])


async def get_row(conn: psycopg.AsyncConnection, campaign_id: int, *,
                  viewer: int) -> dict[str, Any] | None:
    """The bare row for tenancy checks (owner_id, visibility, status)."""
    cur = await conn.execute(
        "SELECT id, owner_id, visibility, status FROM campaign"
        f" WHERE id = %s AND {_VISIBLE}", (campaign_id, viewer))
    return await cur.fetchone()


async def job_id_for(conn: psycopg.AsyncConnection,
                     campaign_id: int) -> int | None:
    cur = await conn.execute(
        "SELECT id FROM job WHERE kind = 'content_generate'"
        " AND payload->>'campaign_id' = %s ORDER BY id DESC LIMIT 1",
        (str(campaign_id),))
    row = await cur.fetchone()
    return int(row["id"]) if row else None


async def get_detail(conn: psycopg.AsyncConnection, campaign_id: int, *,
                     viewer: int) -> CampaignDetail | None:
    cur = await conn.execute(
        "SELECT id, subject, input_type, status, formats, visibility,"
        " owner_id, error, created_at, finished_at FROM campaign"
        f" WHERE id = %s AND {_VISIBLE}", (campaign_id, viewer))
    row = await cur.fetchone()
    if row is None:
        return None
    cur = await conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM job_event"
        " WHERE job_id IN (SELECT id FROM job WHERE kind = 'content_generate'"
        " AND payload->>'campaign_id' = %s)", (str(campaign_id),))
    last_seq = int((await cur.fetchone())["last_seq"])
    items = await item_dao.list_for_campaign(conn, campaign_id, viewer=viewer)
    return CampaignDetail(
        id=row["id"], subject=row["subject"], input_type=row["input_type"],
        status=row["status"], formats=list(row["formats"] or []),
        visibility=row["visibility"], owner_id=row["owner_id"],
        last_seq=last_seq, error=row["error"], items=items,
        created_at=str(row["created_at"]),
        updated_at=str(row["finished_at"]) if row["finished_at"] else None)


async def list_page(conn: psycopg.AsyncConnection, *, viewer: int,
                    limit: int = 50, offset: int = 0,
                    workspace_id: int | None = None) -> CampaignPage:
    # optional scope to one workspace's campaigns (the seed carries the id)
    scope, scope_param = "", ()
    if workspace_id is not None:
        scope = " AND seed->>'workspace_id' = %s"
        scope_param = (str(workspace_id),)
    cur = await conn.execute(
        f"SELECT count(*) AS n FROM campaign WHERE {_VISIBLE}{scope}",
        (viewer, *scope_param))
    total = int((await cur.fetchone())["n"])
    cur = await conn.execute(
        "SELECT c.id, c.subject, c.input_type, c.status, c.formats,"
        " c.created_at, (SELECT count(*) FROM content_item ci"
        "   WHERE ci.campaign_id = c.id) AS item_count"
        " FROM campaign c WHERE (c.owner_id = %s OR c.visibility = 'shared')"
        f"{scope}"
        " ORDER BY c.created_at DESC, c.id DESC LIMIT %s OFFSET %s",
        (viewer, *scope_param, limit, offset))
    items = [CampaignListItem(
        id=r["id"], subject=r["subject"], input_type=r["input_type"],
        status=r["status"], formats=list(r["formats"] or []),
        item_count=int(r["item_count"]), created_at=str(r["created_at"]))
        for r in await cur.fetchall()]
    return CampaignPage(items=items, total=total)


async def delete(conn: psycopg.AsyncConnection, campaign_id: int, *,
                 owner_id: int) -> bool:
    """Delete a campaign (its content_items cascade via the FK). Owner-scoped;
    returns False when nothing was deleted."""
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM campaign WHERE id = %s AND owner_id = %s",
            (campaign_id, owner_id))
        return cur.rowcount > 0


async def set_status(conn: psycopg.AsyncConnection, campaign_id: int,
                     status: str, *, started: bool = False,
                     finished: bool = False, error: str | None = None) -> None:
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
    params.append(campaign_id)
    async with conn.transaction():
        await conn.execute(
            f"UPDATE campaign SET {', '.join(sets)} WHERE id = %s", params)
