"""Entity read DAO — raw SQL, returns domain contracts.

last/first_seen use COALESCE(published_at, fetched_at) of mention-joined
documents (when the entity last appeared in the corpus, not when the
extractor ran). Co-occurrence is computed live with the lift normalizer
(together / other's document frequency) so everything doesn't co-occur with
the PM — at single-user corpus scale this is milliseconds.
"""

from __future__ import annotations

import json
import sqlite3

from connect.domain.models import (
    CoOccurringEntity,
    DocumentListItem,
    EntityDetail,
    EntityInfo,
    EntityListItem,
    TopicCount,
)
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


def _to_list_entity(row: sqlite3.Row) -> EntityListItem:
    return EntityListItem(
        id=row["id"],
        name=row["name"],
        entity_type=row["entity_type"],
        mention_count=row["mention_count"] or 0,
        document_count=row["document_count"] or 0,
        last_seen_at=row["last_seen_at"],
    )


def list_page(conn: sqlite3.Connection, *, q: str | None = None,
              page: int = 1, page_size: int = 20,
              ) -> tuple[list[EntityListItem], int]:
    """Mention-count-ordered entity listing; q matches name OR alias."""
    where, params = "", []
    if q:
        where = ("WHERE (e.name LIKE ? ESCAPE '\\'"
                 " OR e.aliases LIKE ? ESCAPE '\\')")
        pattern = f"%{_like_escape(q)}%"
        params = [pattern, pattern]
    total = conn.execute(
        f"SELECT COUNT(*) FROM entity e {where}", params).fetchone()[0]
    rows = conn.execute(
        f"WITH {_STATS_CTE}"
        f" SELECT e.id, e.name, e.entity_type, st.mention_count,"
        f" st.document_count, st.last_seen_at"
        f" FROM entity e JOIN entity_stats st ON st.entity_id = e.id"
        f" {where}"
        f" ORDER BY st.mention_count DESC, e.name ASC"
        f" LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size)).fetchall()
    return [_to_list_entity(r) for r in rows], int(total)


def search(conn: sqlite3.Connection, q: str, *,
           limit: int = 20) -> tuple[list[EntityListItem], int]:
    items, total = list_page(conn, q=q, page=1, page_size=limit)
    return items, total


def get_detail(conn: sqlite3.Connection, entity_id: int, *,
               co_occurring_limit: int = 20,
               documents_limit: int = 10) -> EntityDetail | None:
    row = conn.execute(
        f"WITH {_STATS_CTE}"
        f" SELECT e.*, st.mention_count, st.document_count,"
        f" st.first_seen_at, st.last_seen_at"
        f" FROM entity e JOIN entity_stats st ON st.entity_id = e.id"
        f" WHERE e.id = ?", (entity_id,)).fetchone()
    if row is None:
        return None
    try:
        aliases = [a for a in json.loads(row["aliases"] or "[]")
                   if isinstance(a, str)]
    except (ValueError, TypeError):
        aliases = []
    return EntityDetail(
        entity=EntityInfo(
            id=row["id"], name=row["name"], entity_type=row["entity_type"],
            aliases=aliases, description=row["description"],
            created_at=row["created_at"]),
        mention_count=row["mention_count"] or 0,
        document_count=row["document_count"] or 0,
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        topics=topics_for(conn, entity_id),
        co_occurring=co_occurring(conn, entity_id,
                                  limit=co_occurring_limit),
        documents=documents_for(conn, entity_id, page=1,
                                page_size=documents_limit)[0],
    )


def topics_for(conn: sqlite3.Connection, entity_id: int) -> list[TopicCount]:
    """Topic distribution over the documents this entity is mentioned in."""
    rows = conn.execute(
        "SELECT dt.topic, COUNT(DISTINCT dt.document_id) AS n"
        " FROM document_topic dt"
        " WHERE dt.document_id IN"
        "   (SELECT document_id FROM entity_mention WHERE entity_id = ?)"
        " GROUP BY dt.topic ORDER BY n DESC, dt.topic ASC",
        (entity_id,)).fetchall()
    return [TopicCount(topic=r["topic"], count=r["n"]) for r in rows]


def co_occurring(conn: sqlite3.Connection, entity_id: int, *,
                 limit: int = 20,
                 min_together: int = 2) -> list[CoOccurringEntity]:
    """Entities sharing documents with this one, lift-normalized
    (design-doc SQL: together / other's total document frequency)."""
    rows = conn.execute(
        f"""
WITH pairs AS (
    SELECT m2.entity_id AS other_id,
           COUNT(DISTINCT m1.document_id) AS together
    FROM entity_mention m1
    JOIN entity_mention m2
      ON m2.document_id = m1.document_id AND m2.entity_id <> m1.entity_id
    WHERE m1.entity_id = ?
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
WHERE p.together >= ?
ORDER BY lift DESC, p.together DESC, e.name ASC
LIMIT ?""",
        (entity_id, min_together, limit)).fetchall()
    return [
        CoOccurringEntity(entity=_to_list_entity(r), together=r["together"],
                          lift=float(r["lift"]))
        for r in rows
    ]


def documents_for(conn: sqlite3.Connection, entity_id: int, *,
                  page: int = 1, page_size: int = 20,
                  ) -> tuple[list[DocumentListItem], int]:
    """Documents mentioning the entity, newest first."""
    total = conn.execute(
        "SELECT COUNT(DISTINCT document_id) FROM entity_mention"
        " WHERE entity_id = ?", (entity_id,)).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_LIST_COLS}"
        f" FROM document d"
        f" LEFT JOIN source s ON s.id = d.source_id"
        f" WHERE d.id IN"
        f"   (SELECT document_id FROM entity_mention WHERE entity_id = ?)"
        f" ORDER BY COALESCE(d.published_at, d.fetched_at) DESC, d.id DESC"
        f" LIMIT ? OFFSET ?",
        (entity_id, page_size, (page - 1) * page_size)).fetchall()
    return [_to_list_item(r) for r in rows], int(total)


def exists(conn: sqlite3.Connection, entity_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM entity WHERE id = ?", (entity_id,)).fetchone() is not None
