"""Post (findings board) DAO — user-authored notes, owned + shared/private.

Reads are viewer-scoped exactly like documents/dossiers: a private post is
visible only to its owner (another user reads it as absent -> 404 at the
router). Writes (patch/delete) are owner-scoped. The linked document's title
and the owner's name are joined in for the feed UI.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import Post
from connect.storage.pg import utc_now

# viewer-scoped visibility: shared posts, or the viewer's own private posts.
VISIBLE_SQL = "(p.visibility = 'shared' OR p.owner_id = %s)"

_SELECT = """
SELECT p.id, p.title, p.body, p.document_id, p.visibility, p.owner_id,
       p.created_at, p.updated_at,
       d.title AS document_title, u.name AS owner_name
FROM post p
LEFT JOIN document d ON d.id = p.document_id
LEFT JOIN app_user u ON u.id = p.owner_id
"""


def _to_model(row: Mapping[str, Any]) -> Post:
    return Post(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        document_id=row["document_id"],
        document_title=row.get("document_title"),
        visibility=row["visibility"],
        owner_id=row["owner_id"],
        owner_name=row.get("owner_name"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def insert(conn: psycopg.AsyncConnection, *, owner_id: int, title: str,
                 body: str, document_id: int | None = None,
                 visibility: str = "shared") -> Post:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO post (owner_id, title, body, document_id,"
            " visibility, created_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (owner_id, title, body, document_id, visibility, utc_now()))
        post_id = (await cur.fetchone())["id"]
    got = await get(conn, post_id)
    assert got is not None
    return got


async def get(conn: psycopg.AsyncConnection, post_id: int, *,
              viewer: int | None = None) -> Post | None:
    sql = _SELECT + " WHERE p.id = %s"
    params: list[Any] = [post_id]
    if viewer is not None:
        sql += " AND " + VISIBLE_SQL
        params.append(viewer)
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def list_visible(conn: psycopg.AsyncConnection, *,
                       viewer: int | None = None,
                       document_id: int | None = None,
                       limit: int = 100, offset: int = 0) -> list[Post]:
    """Newest-first posts the viewer may see, optionally only those tied to
    one document."""
    sql = _SELECT
    where: list[str] = []
    params: list[Any] = []
    if viewer is not None:
        where.append(VISIBLE_SQL)
        params.append(viewer)
    if document_id is not None:
        where.append("p.document_id = %s")
        params.append(document_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.created_at DESC, p.id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    cur = await conn.execute(sql, params)
    return [_to_model(r) for r in await cur.fetchall()]


_PATCHABLE = ("title", "body", "visibility")


async def update(conn: psycopg.AsyncConnection, post_id: int,
                 fields: dict[str, Any], *,
                 owner_id: int | None = None) -> Post | None:
    sets: list[str] = []
    params: list[Any] = []
    for col in _PATCHABLE:
        if col in fields and fields[col] is not None:
            sets.append(f"{col} = %s")
            params.append(fields[col])
    if sets:
        sets.append("updated_at = %s")
        params.append(utc_now())
        sql = f"UPDATE post SET {', '.join(sets)} WHERE id = %s"
        params.append(post_id)
        if owner_id is not None:
            sql += " AND owner_id = %s"
            params.append(owner_id)
        async with conn.transaction():
            await conn.execute(sql, params)
    # re-read with owner as viewer so the owner always sees their own row
    return await get(conn, post_id, viewer=owner_id)


async def delete(conn: psycopg.AsyncConnection, post_id: int, *,
                 owner_id: int | None = None) -> bool:
    sql, params = "DELETE FROM post WHERE id = %s", [post_id]
    if owner_id is not None:
        sql += " AND owner_id = %s"
        params.append(owner_id)
    async with conn.transaction():
        cur = await conn.execute(sql, params)
    return cur.rowcount > 0
