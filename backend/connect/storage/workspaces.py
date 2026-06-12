"""Workspace DAO — a saved lens over the shared corpus.

A workspace is owned + shared/private (read-scoped like documents/dossiers).
``focused_feed`` is the lens in action: documents matching ANY of the
workspace's focus signals (registered sources, T1 topics, or an FTS query),
newest first, viewer-scoped. It reuses the document listing internals so the
row shape matches the rest of the API.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import psycopg

from connect.domain.models import DocumentListItem, Workspace
from connect.storage import documents as doc_dao
from connect.storage.pg import Jsonb, utc_now

# viewer-scoped visibility: shared workspaces, or the viewer's own private ones.
VISIBLE_SQL = "(w.visibility = 'shared' OR w.owner_id = %s)"

_SELECT = """
SELECT w.id, w.name, w.description, w.topics, w.source_ids, w.query_fts,
       w.visibility, w.owner_id, w.created_at, w.updated_at,
       u.name AS owner_name
FROM workspace w
LEFT JOIN app_user u ON u.id = w.owner_id
"""


def _to_model(row: Mapping[str, Any]) -> Workspace:
    return Workspace(
        id=row["id"],
        name=row["name"],
        description=row["description"] or "",
        topics=list(row["topics"] or []),
        source_ids=list(row["source_ids"] or []),
        query_fts=row["query_fts"],
        visibility=row["visibility"],
        owner_id=row["owner_id"],
        owner_name=row.get("owner_name"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def insert(conn: psycopg.AsyncConnection, *, owner_id: int, name: str,
                 description: str = "", topics: Iterable[str] = (),
                 source_ids: Iterable[int] = (), query_fts: str | None = None,
                 visibility: str = "shared") -> Workspace:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO workspace (owner_id, name, description, topics,"
            " source_ids, query_fts, visibility, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (owner_id, name, description, Jsonb(list(topics)),
             Jsonb([int(s) for s in source_ids]), query_fts, visibility,
             utc_now()))
        workspace_id = (await cur.fetchone())["id"]
    got = await get(conn, workspace_id)
    assert got is not None
    return got


async def get(conn: psycopg.AsyncConnection, workspace_id: int, *,
              viewer: int | None = None) -> Workspace | None:
    sql = _SELECT + " WHERE w.id = %s"
    params: list[Any] = [workspace_id]
    if viewer is not None:
        sql += " AND " + VISIBLE_SQL
        params.append(viewer)
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def list_visible(conn: psycopg.AsyncConnection, *,
                       viewer: int | None = None) -> list[Workspace]:
    sql = _SELECT
    params: list[Any] = []
    if viewer is not None:
        sql += " WHERE " + VISIBLE_SQL
        params.append(viewer)
    sql += " ORDER BY w.created_at DESC, w.id DESC"
    cur = await conn.execute(sql, params)
    return [_to_model(r) for r in await cur.fetchall()]


_PATCHABLE = ("name", "description", "query_fts", "visibility")
_PATCHABLE_JSON = ("topics", "source_ids")


async def update(conn: psycopg.AsyncConnection, workspace_id: int,
                 fields: dict[str, Any], *,
                 owner_id: int | None = None) -> Workspace | None:
    sets: list[str] = []
    params: list[Any] = []
    for col in _PATCHABLE:
        if col in fields and fields[col] is not None:
            sets.append(f"{col} = %s")
            params.append(fields[col])
    for col in _PATCHABLE_JSON:
        if col in fields and fields[col] is not None:
            sets.append(f"{col} = %s")
            params.append(Jsonb(list(fields[col])))
    if sets:
        sets.append("updated_at = %s")
        params.append(utc_now())
        sql = f"UPDATE workspace SET {', '.join(sets)} WHERE id = %s"
        params.append(workspace_id)
        if owner_id is not None:
            sql += " AND owner_id = %s"
            params.append(owner_id)
        async with conn.transaction():
            await conn.execute(sql, params)
    return await get(conn, workspace_id, viewer=owner_id)


async def delete(conn: psycopg.AsyncConnection, workspace_id: int, *,
                 owner_id: int | None = None) -> bool:
    sql, params = "DELETE FROM workspace WHERE id = %s", [workspace_id]
    if owner_id is not None:
        sql += " AND owner_id = %s"
        params.append(owner_id)
    async with conn.transaction():
        cur = await conn.execute(sql, params)
    return cur.rowcount > 0


async def focused_feed(conn: psycopg.AsyncConnection, workspace: Workspace, *,
                       viewer: int | None = None, page: int = 1,
                       page_size: int = 20,
                       ) -> tuple[list[DocumentListItem], int]:
    """Documents matching ANY of the workspace's focus signals (sources /
    topics / FTS query), newest first. With no focus configured it falls back
    to the whole visible feed (a fresh workspace starts broad)."""
    where: list[str] = []
    params: list[Any] = []
    if viewer is not None:
        where.append(doc_dao.VISIBLE_SQL)
        params.append(viewer)

    focus: list[str] = []
    if workspace.source_ids:
        focus.append("d.source_id = ANY(%s)")
        params.append([int(s) for s in workspace.source_ids])
    if workspace.topics:
        focus.append("EXISTS (SELECT 1 FROM document_topic dt"
                     " WHERE dt.document_id = d.id AND dt.topic = ANY(%s))")
        params.append(list(workspace.topics))
    if workspace.query_fts:
        focus.append("d.search_tsv @@ websearch_to_tsquery('english', %s)")
        params.append(workspace.query_fts)
    if focus:
        where.append("(" + " OR ".join(focus) + ")")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    cur = await conn.execute(
        "SELECT COUNT(*) AS n" + doc_dao._FROM + where_sql, params)
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT " + doc_dao._LIST_COLS + doc_dao._FROM + where_sql
        + " ORDER BY d.fetched_at DESC, d.id DESC LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    return [doc_dao._to_list_item(r) for r in rows], int(total)
