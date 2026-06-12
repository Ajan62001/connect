"""Social-post drafts the workspace agent generates — owner-scoped, workspace-
tagged. ``content`` is the SocialPost; ``card_sha`` references the rendered
card in the social card store.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import SocialDraft, SocialPost
from connect.storage.pg import Jsonb, utc_now


def _to_model(row: Mapping[str, Any]) -> SocialDraft:
    return SocialDraft(
        id=row["id"],
        workspace_id=row["workspace_id"],
        document_id=row["document_id"],
        content=SocialPost(**dict(row["content"] or {})),
        card_sha=row["card_sha"],
        created_at=row["created_at"],
    )


async def insert(conn: psycopg.AsyncConnection, *, workspace_id: int,
                 owner_id: int, document_id: int | None,
                 content: dict[str, Any], card_sha: str) -> int:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO social_draft (workspace_id, owner_id, document_id,"
            " content, card_sha, created_at) VALUES (%s,%s,%s,%s,%s,%s)"
            " RETURNING id",
            (workspace_id, owner_id, document_id, Jsonb(content), card_sha,
             utc_now()))
        return int((await cur.fetchone())["id"])


async def list_for(conn: psycopg.AsyncConnection, workspace_id: int, *,
                   owner_id: int) -> list[SocialDraft]:
    cur = await conn.execute(
        "SELECT id, workspace_id, document_id, content, card_sha, created_at"
        " FROM social_draft WHERE workspace_id = %s AND owner_id = %s"
        " ORDER BY created_at DESC, id DESC", (workspace_id, owner_id))
    return [_to_model(r) for r in await cur.fetchall()]


async def delete(conn: psycopg.AsyncConnection, draft_id: int, *,
                 owner_id: int) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM social_draft WHERE id = %s AND owner_id = %s",
            (draft_id, owner_id))
    return cur.rowcount > 0
