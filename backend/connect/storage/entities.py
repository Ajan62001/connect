"""Entity read DAO — raw SQL, returns domain contracts.

last/first_seen use COALESCE(published_at, fetched_at) of mention-joined
documents (when the entity last appeared in the corpus, not when the
extractor ran). Co-occurrence is computed live with the lift normalizer
(together / other's document frequency) so everything doesn't co-occur with
the PM — at single-user corpus scale this is milliseconds.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import (
    CoOccurringEntity,
    DocumentListItem,
    EntityDelta,
    EntityDetail,
    EntityInfo,
    EntityListItem,
    TopicCount,
)
from connect.storage import cursors as cursor_dao
from connect.storage import events as event_dao
from connect.storage import statements as statement_dao
from connect.storage.documents import _LIST_COLS, _to_list_item

# Shared per-entity stat aggregates (joined onto entity as 'st').
_STATS_CTE = """
entity_stats AS (
    SELECT e.id AS entity_id,
           COUNT(m.id) AS mention_count,
           COUNT(DISTINCT m.document_id) AS document_count,
           MIN(COALESCE(d.published_at, d.fetched_at)) AS first_seen_at,
           MAX(COALESCE(d.published_at, d.fetched_at)) AS last_seen_at
    FROM entity e
    LEFT JOIN entity_mention m ON m.entity_id = e.id
    LEFT JOIN document d ON d.id = m.document_id
    GROUP BY e.id
)
"""


def _like_escape(q: str) -> str:
    return (q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def _to_list_entity(row: Mapping[str, Any]) -> EntityListItem:
    return EntityListItem(
        id=row["id"],
        name=row["name"],
        entity_type=row["entity_type"],
        mention_count=row["mention_count"] or 0,
        document_count=row["document_count"] or 0,
        last_seen_at=row["last_seen_at"],
    )


async def list_page(conn: psycopg.AsyncConnection, *, q: str | None = None,
                    page: int = 1, page_size: int = 20,
                    ) -> tuple[list[EntityListItem], int]:
    """Mention-count-ordered entity listing; q matches name OR alias.

    ILIKE (SQLite LIKE was case-insensitive for ASCII — PG's is not; the
    sneakiest SQLite-ism in the codebase). Substring search over the
    aliases jsonb goes through its ::text cast — served by the trigram GIN
    indexes at scale.
    """
    where, params = "", []
    if q:
        where = (r"WHERE (e.name ILIKE %s ESCAPE '\'"
                 r" OR e.aliases::text ILIKE %s ESCAPE '\')")
        pattern = f"%{_like_escape(q)}%"
        params = [pattern, pattern]
    cur = await conn.execute(
        f"SELECT COUNT(*) AS n FROM entity e {where}", params)
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        f"WITH {_STATS_CTE}"
        f" SELECT e.id, e.name, e.entity_type, st.mention_count,"
        f" st.document_count, st.last_seen_at"
        f" FROM entity e JOIN entity_stats st ON st.entity_id = e.id"
        f" {where}"
        f" ORDER BY st.mention_count DESC, e.name ASC"
        f" LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    return [_to_list_entity(r) for r in rows], int(total)


async def search(conn: psycopg.AsyncConnection, q: str, *,
                 limit: int = 20) -> tuple[list[EntityListItem], int]:
    items, total = await list_page(conn, q=q, page=1, page_size=limit)
    return items, total


async def get_detail(conn: psycopg.AsyncConnection, entity_id: int, *,
                     co_occurring_limit: int = 20,
                     documents_limit: int = 10,
                     viewer: int | None = None) -> EntityDetail | None:
    """The entity page aggregate. Derived rows (mentions/events/topics)
    need no tenancy predicates — invariant I1 guarantees they reference
    only shared documents; ``viewer`` scopes the per-user view_cursor
    delta only."""
    cur = await conn.execute(
        f"WITH {_STATS_CTE}"
        f" SELECT e.*, st.mention_count, st.document_count,"
        f" st.first_seen_at, st.last_seen_at"
        f" FROM entity e JOIN entity_stats st ON st.entity_id = e.id"
        f" WHERE e.id = %s", (entity_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    aliases = [a for a in (row["aliases"] or []) if isinstance(a, str)]
    return EntityDetail(
        entity=EntityInfo(
            id=row["id"], name=row["name"], entity_type=row["entity_type"],
            aliases=aliases, description=row["description"],
            created_at=row["created_at"]),
        mention_count=row["mention_count"] or 0,
        document_count=row["document_count"] or 0,
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        topics=await topics_for(conn, entity_id),
        co_occurring=await co_occurring(conn, entity_id,
                                        limit=co_occurring_limit),
        documents=(await documents_for(conn, entity_id, page=1,
                                       page_size=documents_limit))[0],
        events=await event_dao.events_for_entity(conn, entity_id, limit=10),
        delta=await delta_since_cursor(conn, entity_id, viewer=viewer),
        has_views=await statement_dao.has_views(conn, entity_id),
    )


async def delta_since_cursor(conn: psycopg.AsyncConnection,
                             entity_id: int, *,
                             viewer: int | None = None,
                             ) -> EntityDelta | None:
    """New events / documents / claims for the entity since the VIEWER's
    view_cursor (surface='entity'); None when never visited (or when there
    is no viewer — cursors are per-user).

    "New" means when the KNOWLEDGE arrived (each row's created_at), not the
    document's publication date — the DeltaBanner answers "what did the KB
    learn since I last looked"."""
    if viewer is None:
        return None
    cursor = await cursor_dao.get(conn, viewer, "entity", entity_id)
    if cursor is None:
        return None
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT document_id) AS n FROM entity_mention"
        " WHERE entity_id = %s AND created_at > %s",
        (entity_id, cursor))
    documents = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT ea.event_id) AS n FROM event_assignment ea"
        " WHERE ea.event_id IS NOT NULL AND ea.created_at > %s"
        " AND ea.document_id IN"
        "   (SELECT document_id FROM entity_mention WHERE entity_id = %s)",
        (cursor, entity_id))
    events = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT cs.claim_id) AS n FROM claim_sighting cs"
        " WHERE cs.created_at > %s AND cs.document_id IN"
        "   (SELECT document_id FROM entity_mention WHERE entity_id = %s)",
        (cursor, entity_id))
    claims = (await cur.fetchone())["n"]
    return EntityDelta(events=int(events), documents=int(documents),
                       claims=int(claims))


async def topics_for(conn: psycopg.AsyncConnection,
                     entity_id: int) -> list[TopicCount]:
    """Topic distribution over the documents this entity is mentioned in."""
    cur = await conn.execute(
        "SELECT dt.topic, COUNT(DISTINCT dt.document_id) AS n"
        " FROM document_topic dt"
        " WHERE dt.document_id IN"
        "   (SELECT document_id FROM entity_mention WHERE entity_id = %s)"
        " GROUP BY dt.topic ORDER BY n DESC, dt.topic ASC",
        (entity_id,))
    rows = await cur.fetchall()
    return [TopicCount(topic=r["topic"], count=r["n"]) for r in rows]


async def co_occurring(conn: psycopg.AsyncConnection, entity_id: int, *,
                       limit: int = 20,
                       min_together: int = 2) -> list[CoOccurringEntity]:
    """Entities sharing documents with this one, lift-normalized
    (design-doc SQL: together / other's total document frequency)."""
    cur = await conn.execute(
        f"""
WITH pairs AS (
    SELECT m2.entity_id AS other_id,
           COUNT(DISTINCT m1.document_id) AS together
    FROM entity_mention m1
    JOIN entity_mention m2
      ON m2.document_id = m1.document_id AND m2.entity_id <> m1.entity_id
    WHERE m1.entity_id = %s
    GROUP BY m2.entity_id
),
{_STATS_CTE}
SELECT e.id, e.name, e.entity_type,
       st.mention_count, st.document_count, st.last_seen_at,
       p.together,
       1.0 * p.together / st.document_count AS lift
FROM pairs p
JOIN entity e ON e.id = p.other_id
JOIN entity_stats st ON st.entity_id = e.id
WHERE p.together >= %s
ORDER BY lift DESC, p.together DESC, e.name ASC
LIMIT %s""",
        (entity_id, min_together, limit))
    rows = await cur.fetchall()
    return [
        CoOccurringEntity(entity=_to_list_entity(r), together=r["together"],
                          lift=float(r["lift"]))
        for r in rows
    ]


async def documents_for(conn: psycopg.AsyncConnection, entity_id: int, *,
                        page: int = 1, page_size: int = 20,
                        ) -> tuple[list[DocumentListItem], int]:
    """Documents mentioning the entity, newest first."""
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT document_id) AS n FROM entity_mention"
        " WHERE entity_id = %s", (entity_id,))
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        f"SELECT {_LIST_COLS}"
        f" FROM document d"
        f" LEFT JOIN source s ON s.id = d.source_id"
        f" WHERE d.id IN"
        f"   (SELECT document_id FROM entity_mention WHERE entity_id = %s)"
        f" ORDER BY COALESCE(d.published_at, d.fetched_at) DESC, d.id DESC"
        f" LIMIT %s OFFSET %s",
        (entity_id, page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    return [_to_list_item(r) for r in rows], int(total)


async def exists(conn: psycopg.AsyncConnection, entity_id: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM entity WHERE id = %s", (entity_id,))
    return await cur.fetchone() is not None
