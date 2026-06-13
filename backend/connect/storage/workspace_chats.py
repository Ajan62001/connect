"""Workspace-agent conversation DAO — saved per user, per workspace.

``messages`` is the provider-shaped wire transcript (replayed to resume the
agent); ``transcript`` is the human-readable display turns the UI renders.
Both are jsonb. Chats are PRIVATE to their owner (every query is owner-scoped).
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.storage.pg import Jsonb, utc_now


def turn_dict(role: str, text: str, *, tools_used: list[str] | None = None,
              finding: Any = None) -> dict[str, Any]:
    """One human-readable transcript turn (ChatTurn shape), shared by the
    synchronous chat endpoint and the async deep-run worker handler."""
    return {"role": role, "text": text,
            "tools_used": tools_used or [],
            "finding_id": finding.id if finding else None,
            "finding_title": finding.title if finding else None}


async def latest_job_id(conn: psycopg.AsyncConnection,
                        chat_id: int) -> int | None:
    """The most recent async deep-run job for a chat (job payload carries the
    chat_id) — the handle the SSE/cancel endpoints resolve to."""
    cur = await conn.execute(
        "SELECT id FROM job WHERE kind = 'workspace_task'"
        " AND (payload->>'chat_id')::bigint = %s ORDER BY id DESC LIMIT 1",
        (chat_id,))
    row = await cur.fetchone()
    return int(row["id"]) if row else None


async def create(conn: psycopg.AsyncConnection, *, workspace_id: int,
                 owner_id: int, title: str, messages: list[Any],
                 transcript: list[Any]) -> int:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO workspace_chat (workspace_id, owner_id, title,"
            " messages, transcript, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (workspace_id, owner_id, title, Jsonb(messages),
             Jsonb(transcript), utc_now()))
        return int((await cur.fetchone())["id"])


async def get(conn: psycopg.AsyncConnection, chat_id: int, *,
              workspace_id: int, owner_id: int) -> Mapping[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, title, messages, transcript, created_at, updated_at"
        " FROM workspace_chat WHERE id = %s AND workspace_id = %s"
        " AND owner_id = %s", (chat_id, workspace_id, owner_id))
    return await cur.fetchone()


async def update(conn: psycopg.AsyncConnection, chat_id: int, *,
                 owner_id: int, messages: list[Any],
                 transcript: list[Any]) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE workspace_chat SET messages = %s, transcript = %s,"
            " updated_at = %s WHERE id = %s AND owner_id = %s",
            (Jsonb(messages), Jsonb(transcript), utc_now(), chat_id,
             owner_id))
    return cur.rowcount > 0


async def list_for(conn: psycopg.AsyncConnection, workspace_id: int,
                   owner_id: int) -> list[Mapping[str, Any]]:
    cur = await conn.execute(
        "SELECT id, title, created_at, updated_at FROM workspace_chat"
        " WHERE workspace_id = %s AND owner_id = %s"
        " ORDER BY COALESCE(updated_at, created_at) DESC, id DESC",
        (workspace_id, owner_id))
    return list(await cur.fetchall())


async def delete(conn: psycopg.AsyncConnection, chat_id: int, *,
                 owner_id: int) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM workspace_chat WHERE id = %s AND owner_id = %s",
            (chat_id, owner_id))
    return cur.rowcount > 0
