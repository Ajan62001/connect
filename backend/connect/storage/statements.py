"""Statement / position-shift read DAO (v9) — raw SQL, returns domain
contracts. statement.topics is a JSON string array; topic filtering uses
json_each (JSON1 ships in every SQLite this codebase targets)."""

from __future__ import annotations

import json
import sqlite3

from connect.domain.models import (
    DocumentStatement,
    EnrichmentEntityRef,
    EntityViewTopic,
    PositionShiftRef,
    PositionShiftRow,
    TopicStatement,
)


def _topics(raw: str | None) -> list[str]:
    try:
        topics = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [t for t in topics if isinstance(t, str)]


def has_views(conn: sqlite3.Connection, entity_id: int) -> bool:
    """Cheap exists-check: does the entity have any statement rows?"""
    return conn.execute(
        "SELECT 1 FROM statement WHERE entity_id = ? LIMIT 1",
        (entity_id,)).fetchone() is not None


def views_for_entity(conn: sqlite3.Connection,
                     entity_id: int) -> list[EntityViewTopic]:
    """Per-topic statement aggregates, statement_count desc. shift_count
    counts OPEN shifts; latest_position is the newest statement's
    position_summary."""
    rows = conn.execute(
        "SELECT je.value AS topic, COUNT(*) AS n,"
        " MIN(s.stated_at) AS first_at, MAX(s.stated_at) AS last_at"
        " FROM statement s, json_each(s.topics) je"
        " WHERE s.entity_id = ?"
        " GROUP BY je.value ORDER BY n DESC, topic ASC",
        (entity_id,)).fetchall()
    shift_counts = {
        r["topic"]: int(r["n"]) for r in conn.execute(
            "SELECT topic, COUNT(*) AS n FROM position_shift"
            " WHERE entity_id = ? AND status = 'open' GROUP BY topic",
            (entity_id,))}
    out: list[EntityViewTopic] = []
    for row in rows:
        latest = conn.execute(
            "SELECT s.position_summary FROM statement s"
            " WHERE s.entity_id = ?"
            " AND EXISTS (SELECT 1 FROM json_each(s.topics) je"
            "             WHERE je.value = ?)"
            " ORDER BY s.stated_at DESC, s.id DESC LIMIT 1",
            (entity_id, row["topic"])).fetchone()
        out.append(EntityViewTopic(
            topic=row["topic"], statement_count=int(row["n"]),
            first_at=row["first_at"], last_at=row["last_at"],
            shift_count=shift_counts.get(row["topic"], 0),
            latest_position=latest["position_summary"] if latest else None))
    return out


def statements_for_topic(conn: sqlite3.Connection, entity_id: int,
                         topic: str) -> list[TopicStatement]:
    """The (entity, topic) statement timeline, newest first, with source
    display fields."""
    rows = conn.execute(
        "SELECT s.id, s.quote, s.position_summary, s.stated_at,"
        " s.document_id, d.url, d.title,"
        " src.name AS source_name, src.credibility_tier"
        " FROM statement s"
        " JOIN document d ON d.id = s.document_id"
        " LEFT JOIN source src ON src.id = d.source_id"
        " WHERE s.entity_id = ?"
        " AND EXISTS (SELECT 1 FROM json_each(s.topics) je"
        "             WHERE je.value = ?)"
        " ORDER BY s.stated_at DESC, s.id DESC",
        (entity_id, topic)).fetchall()
    return [
        TopicStatement(
            id=r["id"], quote=r["quote"],
            position_summary=r["position_summary"],
            stated_at=r["stated_at"], document_id=r["document_id"],
            source_name=r["source_name"],
            credibility_tier=r["credibility_tier"],
            url=r["url"], title=r["title"])
        for r in rows]


def shifts_for_topic(conn: sqlite3.Connection, entity_id: int,
                     topic: str) -> list[PositionShiftRef]:
    """OPEN shifts for the (entity, topic) pair, newest first."""
    rows = conn.execute(
        "SELECT id, kind, note, from_statement_id, to_statement_id,"
        " detected_at FROM position_shift"
        " WHERE entity_id = ? AND topic = ? AND status = 'open'"
        " ORDER BY detected_at DESC, id DESC", (entity_id, topic)).fetchall()
    return [
        PositionShiftRef(
            id=r["id"], kind=r["kind"], note=r["note"],
            from_statement_id=r["from_statement_id"],
            to_statement_id=r["to_statement_id"],
            detected_at=r["detected_at"])
        for r in rows]


def statements_for_document(conn: sqlite3.Connection,
                            document_id: int) -> list[DocumentStatement]:
    rows = conn.execute(
        "SELECT s.id, s.quote, s.topics, s.position_summary,"
        " e.id AS entity_id, e.name, e.entity_type"
        " FROM statement s JOIN entity e ON e.id = s.entity_id"
        " WHERE s.document_id = ? ORDER BY s.id", (document_id,)).fetchall()
    return [
        DocumentStatement(
            id=r["id"],
            speaker=EnrichmentEntityRef(
                id=r["entity_id"], name=r["name"],
                entity_type=r["entity_type"]),
            quote=r["quote"], topics=_topics(r["topics"]),
            position_summary=r["position_summary"])
        for r in rows]


def _to_shift_row(row: sqlite3.Row) -> PositionShiftRow:
    return PositionShiftRow(
        id=row["id"], entity_id=row["entity_id"], topic=row["topic"],
        from_statement_id=row["from_statement_id"],
        to_statement_id=row["to_statement_id"], kind=row["kind"],
        note=row["note"], detected_at=row["detected_at"],
        status=row["status"])


def dismiss_shift(conn: sqlite3.Connection,
                  shift_id: int) -> PositionShiftRow | None:
    """Set status='dismissed'; returns the updated row (None if missing).
    Idempotent — dismissing twice keeps 'dismissed'."""
    with conn:
        cur = conn.execute(
            "UPDATE position_shift SET status = 'dismissed' WHERE id = ?",
            (shift_id,))
    if cur.rowcount == 0:
        return None
    row = conn.execute(
        "SELECT id, entity_id, topic, from_statement_id, to_statement_id,"
        " kind, note, detected_at, status FROM position_shift WHERE id = ?",
        (shift_id,)).fetchone()
    return _to_shift_row(row) if row else None
