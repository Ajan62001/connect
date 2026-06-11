"""Event + story-thread read DAO — raw SQL, returns domain contracts.

An event's documents are its event_assignment members (assignments with a
NULL event_id are pure audit rows for 'new'-decision failures and are never
membership). An event's entities are derived live from member documents'
entity_mention rows — no separate event-entity table to drift.
"""

from __future__ import annotations

import psycopg

from connect.domain.models import (
    DocumentEventRef,
    DocumentListItem,
    EnrichmentEntityRef,
    EntityEventRef,
    EventDetail,
    EventInfo,
    StoryInfo,
    ThreadDetail,
    ThreadEventRef,
)
from connect.storage.documents import _LIST_COLS, _to_list_item

MAX_EVENT_ENTITIES = 15


async def get_event(conn: psycopg.AsyncConnection,
                    event_id: int) -> EventInfo | None:
    cur = await conn.execute(
        "SELECT id, title, description, event_type, occurred_on, doc_count,"
        " story_id, geo_scope FROM event WHERE id = %s",
        (event_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return EventInfo(
        id=row["id"], title=row["title"], summary=row["description"],
        event_type=row["event_type"], occurred_on=row["occurred_on"],
        doc_count=row["doc_count"] or 0, story_id=row["story_id"],
        geo_scope=row["geo_scope"])


async def get_event_detail(conn: psycopg.AsyncConnection,
                           event_id: int) -> EventDetail | None:
    event = await get_event(conn, event_id)
    if event is None:
        return None
    return EventDetail(
        event=event,
        documents=await documents_for_event(conn, event_id),
        entities=await entities_for_events(conn, [event_id]))


async def documents_for_event(conn: psycopg.AsyncConnection,
                              event_id: int) -> list[DocumentListItem]:
    cur = await conn.execute(
        f"SELECT {_LIST_COLS}"
        f" FROM document d LEFT JOIN source s ON s.id = d.source_id"
        f" WHERE d.id IN (SELECT document_id FROM event_assignment"
        f"                WHERE event_id = %s)"
        f" ORDER BY COALESCE(d.published_at, d.fetched_at) DESC, d.id DESC",
        (event_id,))
    return [_to_list_item(r) for r in await cur.fetchall()]


async def entities_for_events(conn: psycopg.AsyncConnection,
                              event_ids: list[int],
                              limit: int = MAX_EVENT_ENTITIES,
                              ) -> list[EnrichmentEntityRef]:
    """Entities mentioned across the events' member documents, most-mentioned
    first (ties by name)."""
    if not event_ids:
        return []
    cur = await conn.execute(
        "SELECT e.id, e.name, e.entity_type, COUNT(*) AS n"
        " FROM entity_mention m JOIN entity e ON e.id = m.entity_id"
        " WHERE m.document_id IN (SELECT document_id FROM event_assignment"
        "                         WHERE event_id = ANY(%s))"
        " GROUP BY e.id, e.name, e.entity_type"
        " ORDER BY n DESC, e.name ASC LIMIT %s",
        (list(event_ids), limit))
    return [EnrichmentEntityRef(id=r["id"], name=r["name"],
                                entity_type=r["entity_type"])
            for r in await cur.fetchall()]


async def get_thread_detail(conn: psycopg.AsyncConnection,
                            story_id: int) -> ThreadDetail | None:
    cur = await conn.execute(
        "SELECT id, title, status, doc_count, summary_text, updated_at"
        " FROM story WHERE id = %s", (story_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    cur = await conn.execute(
        "SELECT id, title, event_type, occurred_on, doc_count, created_at"
        " FROM event WHERE story_id = %s"
        " ORDER BY COALESCE(occurred_on,"
        "   (created_at AT TIME ZONE 'utc')::date) ASC, id ASC",
        (story_id,))
    event_rows = await cur.fetchall()
    events = [
        ThreadEventRef(id=r["id"], title=r["title"],
                       event_type=r["event_type"],
                       occurred_on=r["occurred_on"],
                       doc_count=r["doc_count"] or 0)
        for r in event_rows]
    return ThreadDetail(
        story=StoryInfo(id=row["id"], title=row["title"],
                        status=row["status"],
                        doc_count=row["doc_count"] or 0,
                        updated_at=row["updated_at"]),
        events=events,
        entities=await entities_for_events(conn, [e.id for e in events]),
        summary=row["summary_text"])


async def events_for_entity(conn: psycopg.AsyncConnection, entity_id: int,
                            limit: int = 10) -> list[EntityEventRef]:
    """Most recent events whose member documents mention the entity."""
    cur = await conn.execute(
        "SELECT DISTINCT ev.id, ev.title, ev.event_type, ev.occurred_on,"
        " COALESCE(ev.occurred_on,"
        "   (ev.created_at AT TIME ZONE 'utc')::date) AS day"
        " FROM event ev JOIN event_assignment ea ON ea.event_id = ev.id"
        " WHERE ea.document_id IN"
        "   (SELECT document_id FROM entity_mention WHERE entity_id = %s)"
        " ORDER BY day DESC, ev.id DESC LIMIT %s",
        (entity_id, limit))
    return [EntityEventRef(id=r["id"], title=r["title"],
                           event_type=r["event_type"],
                           occurred_on=r["occurred_on"])
            for r in await cur.fetchall()]


async def event_for_document(conn: psycopg.AsyncConnection,
                             document_id: int) -> DocumentEventRef | None:
    """The event this document was clustered into (latest assignment wins)."""
    cur = await conn.execute(
        "SELECT e.id, e.title FROM event_assignment ea"
        " JOIN event e ON e.id = ea.event_id"
        " WHERE ea.document_id = %s AND ea.event_id IS NOT NULL"
        " ORDER BY ea.id DESC LIMIT 1", (document_id,))
    row = await cur.fetchone()
    return DocumentEventRef(id=row["id"], title=row["title"]) if row else None
