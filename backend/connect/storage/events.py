"""Event + story-thread read DAO — raw SQL, returns domain contracts.

An event's documents are its event_assignment members (assignments with a
NULL event_id are pure audit rows for 'new'-decision failures and are never
membership). An event's entities are derived live from member documents'
entity_mention rows — no separate event-entity table to drift.
"""

from __future__ import annotations

import sqlite3

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


def get_event(conn: sqlite3.Connection, event_id: int) -> EventInfo | None:
    row = conn.execute(
        "SELECT id, title, description, event_type, occurred_on, doc_count,"
        " story_id, geo_scope FROM event WHERE id = ?",
        (event_id,)).fetchone()
    if row is None:
        return None
    return EventInfo(
        id=row["id"], title=row["title"], summary=row["description"],
        event_type=row["event_type"], occurred_on=row["occurred_on"],
        doc_count=row["doc_count"] or 0, story_id=row["story_id"],
        geo_scope=row["geo_scope"])


def get_event_detail(conn: sqlite3.Connection,
                     event_id: int) -> EventDetail | None:
    event = get_event(conn, event_id)
    if event is None:
        return None
    return EventDetail(
        event=event,
        documents=documents_for_event(conn, event_id),
        entities=entities_for_events(conn, [event_id]))


def documents_for_event(conn: sqlite3.Connection,
                        event_id: int) -> list[DocumentListItem]:
    rows = conn.execute(
        f"SELECT {_LIST_COLS}"
        f" FROM document d LEFT JOIN source s ON s.id = d.source_id"
        f" WHERE d.id IN (SELECT document_id FROM event_assignment"
        f"                WHERE event_id = ?)"
        f" ORDER BY COALESCE(d.published_at, d.fetched_at) DESC, d.id DESC",
        (event_id,)).fetchall()
    return [_to_list_item(r) for r in rows]


def entities_for_events(conn: sqlite3.Connection, event_ids: list[int],
                        limit: int = MAX_EVENT_ENTITIES,
                        ) -> list[EnrichmentEntityRef]:
    """Entities mentioned across the events' member documents, most-mentioned
    first (ties by name)."""
    if not event_ids:
        return []
    marks = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"SELECT e.id, e.name, e.entity_type, COUNT(*) AS n"
        f" FROM entity_mention m JOIN entity e ON e.id = m.entity_id"
        f" WHERE m.document_id IN (SELECT document_id FROM event_assignment"
        f"                         WHERE event_id IN ({marks}))"
        f" GROUP BY e.id ORDER BY n DESC, e.name ASC LIMIT ?",
        (*event_ids, limit)).fetchall()
    return [EnrichmentEntityRef(id=r["id"], name=r["name"],
                                entity_type=r["entity_type"]) for r in rows]


def get_thread_detail(conn: sqlite3.Connection,
                      story_id: int) -> ThreadDetail | None:
    row = conn.execute(
        "SELECT id, title, status, doc_count, summary_text, updated_at"
        " FROM story WHERE id = ?", (story_id,)).fetchone()
    if row is None:
        return None
    event_rows = conn.execute(
        "SELECT id, title, event_type, occurred_on, doc_count, created_at"
        " FROM event WHERE story_id = ?"
        " ORDER BY COALESCE(occurred_on, substr(created_at, 1, 10)) ASC,"
        " id ASC", (story_id,)).fetchall()
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
        entities=entities_for_events(conn, [e.id for e in events]),
        summary=row["summary_text"])


def events_for_entity(conn: sqlite3.Connection, entity_id: int,
                      limit: int = 10) -> list[EntityEventRef]:
    """Most recent events whose member documents mention the entity."""
    rows = conn.execute(
        "SELECT DISTINCT ev.id, ev.title, ev.event_type, ev.occurred_on,"
        " ev.created_at"
        " FROM event ev JOIN event_assignment ea ON ea.event_id = ev.id"
        " WHERE ea.document_id IN"
        "   (SELECT document_id FROM entity_mention WHERE entity_id = ?)"
        " ORDER BY COALESCE(ev.occurred_on, substr(ev.created_at, 1, 10))"
        " DESC, ev.id DESC LIMIT ?",
        (entity_id, limit)).fetchall()
    return [EntityEventRef(id=r["id"], title=r["title"],
                           event_type=r["event_type"],
                           occurred_on=r["occurred_on"]) for r in rows]


def event_for_document(conn: sqlite3.Connection,
                       document_id: int) -> DocumentEventRef | None:
    """The event this document was clustered into (latest assignment wins)."""
    row = conn.execute(
        "SELECT e.id, e.title FROM event_assignment ea"
        " JOIN event e ON e.id = ea.event_id"
        " WHERE ea.document_id = ? AND ea.event_id IS NOT NULL"
        " ORDER BY ea.id DESC LIMIT 1", (document_id,)).fetchone()
    return DocumentEventRef(id=row["id"], title=row["title"]) if row else None
