"""Story DAO — read a story dossier (kind='story') + its synthesize section.

Reads are viewer-scoped exactly like the other dossiers (owner OR shared; a
private story another user cannot see reads as absent -> 404 at the router).
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.storage.pg import Jsonb, utc_now
from connect.story.schema import (
    StoryDetail,
    StoryGrounding,
    StoryListItem,
    StoryPage,
    StorySource,
)

_VISIBLE = "(owner_id = %s OR visibility = 'shared')"


async def get_detail(conn: psycopg.AsyncConnection, story_id: int, *,
                     viewer: int) -> StoryDetail | None:
    cur = await conn.execute(
        "SELECT id, title, input_text, input_type, status, owner_id,"
        " visibility, error, created_at, finished_at"
        f" FROM dossier WHERE id = %s AND kind = 'story' AND {_VISIBLE}",
        (story_id, viewer))
    row = await cur.fetchone()
    if row is None:
        return None

    cur = await conn.execute(
        "SELECT content FROM dossier_section WHERE dossier_id = %s"
        " AND stage = 'synthesize' AND status = 'completed'", (story_id,))
    sec = await cur.fetchone()
    content: dict[str, Any] = (sec["content"] if sec
                               and isinstance(sec["content"], dict) else {})

    cur = await conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM job_event"
        " WHERE job_id IN (SELECT id FROM job WHERE dossier_id = %s)",
        (story_id,))
    last_seq = int((await cur.fetchone())["last_seq"])

    sources = [StorySource(**s) for s in content.get("sources", [])
               if isinstance(s, dict)]
    grounding = (StoryGrounding(**content["grounding"])
                 if isinstance(content.get("grounding"), dict) else None)
    return StoryDetail(
        id=row["id"], title=row["title"] or content.get("title"),
        subject=row["input_text"], input_type=row["input_type"],
        status=row["status"], narrative_md=content.get("narrative_md"),
        sources=sources, grounding=grounding, visibility=row["visibility"],
        owner_id=row["owner_id"], last_seq=last_seq, error=row["error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["finished_at"]) if row["finished_at"] else None)


async def fact_context(conn: psycopg.AsyncConnection,
                       story_id: int) -> str | None:
    """Render the story's closed fact-set (the 'scope' section) as a context
    block for grounded Q&A. None when the story has no gathered facts."""
    cur = await conn.execute(
        "SELECT content FROM dossier_section WHERE dossier_id = %s"
        " AND stage = 'scope'", (story_id,))
    row = await cur.fetchone()
    content = row["content"] if row and isinstance(row["content"], dict) else {}
    facts = content.get("facts") or []
    if not facts:
        return None
    lines = [f"SUBJECT: {content.get('subject', '')}", "", "FACTS:"]
    for i, f in enumerate(facts, 1):
        if not isinstance(f, dict):
            continue
        src = f.get("source_name") or "source"
        when = f" ({f['occurred_on']})" if f.get("occurred_on") else ""
        lines.append(f"[{i}] ({src}{when}) \"{f.get('quote', '')}\"")
    return "\n".join(lines)


async def update_narrative(conn: psycopg.AsyncConnection, story_id: int, *,
                           owner_id: int, title: str | None,
                           narrative_md: str | None) -> bool:
    """Hand-edit the generated narrative (owner-only). Patches the 'synthesize'
    section content + the dossier title, and flags grounding.edited. Returns
    False when the story is not the owner's or has no narrative yet."""
    cur = await conn.execute(
        "SELECT s.content FROM dossier_section s JOIN dossier d"
        " ON d.id = s.dossier_id WHERE s.dossier_id = %s AND s.stage ="
        " 'synthesize' AND d.kind = 'story' AND d.owner_id = %s",
        (story_id, owner_id))
    row = await cur.fetchone()
    if row is None:
        return False
    content = dict(row["content"]) if isinstance(row["content"], dict) else {}
    if title is not None:
        content["title"] = title
    if narrative_md is not None:
        content["narrative_md"] = narrative_md
    grounding = dict(content.get("grounding") or {})
    grounding["edited"] = True
    content["grounding"] = grounding
    now = utc_now()
    async with conn.transaction():
        await conn.execute(
            "UPDATE dossier_section SET content = %s, updated_at = %s"
            " WHERE dossier_id = %s AND stage = 'synthesize'",
            (Jsonb(content), now, story_id))
        if title is not None:
            await conn.execute(
                "UPDATE dossier SET title = %s WHERE id = %s",
                (title[:300], story_id))
    return True


async def list_page(conn: psycopg.AsyncConnection, *, viewer: int,
                    limit: int = 50, offset: int = 0) -> StoryPage:
    cur = await conn.execute(
        f"SELECT count(*) AS n FROM dossier WHERE kind = 'story' AND {_VISIBLE}",
        (viewer,))
    total = int((await cur.fetchone())["n"])
    cur = await conn.execute(
        "SELECT id, title, input_text, input_type, status, created_at"
        f" FROM dossier WHERE kind = 'story' AND {_VISIBLE}"
        " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
        (viewer, limit, offset))
    items = [StoryListItem(
        id=r["id"], title=r["title"], subject=r["input_text"],
        input_type=r["input_type"], status=r["status"],
        created_at=str(r["created_at"])) for r in await cur.fetchall()]
    return StoryPage(items=items, total=total)
