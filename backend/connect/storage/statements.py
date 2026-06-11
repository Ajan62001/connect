"""Statement / position-shift read DAO (v9) — raw SQL, returns domain
contracts. statement.topics is a jsonb string array; topic filtering uses
jsonb_array_elements_text (the json_each of Postgres)."""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import (
    DocumentStatement,
    EnrichmentEntityRef,
    EntityViewTopic,
    PositionShiftRef,
    PositionShiftRow,
    TopicStatement,
)


def _topics(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str)]


async def has_views(conn: psycopg.AsyncConnection, entity_id: int) -> bool:
    """Cheap exists-check: does the entity have any statement rows?"""
    cur = await conn.execute(
        "SELECT 1 FROM statement WHERE entity_id = %s LIMIT 1",
        (entity_id,))
    return await cur.fetchone() is not None


async def views_for_entity(conn: psycopg.AsyncConnection,
                           entity_id: int) -> list[EntityViewTopic]:
    """Per-topic statement aggregates, statement_count desc. shift_count
    counts OPEN shifts; latest_position is the newest statement's
    position_summary."""
    cur = await conn.execute(
        "SELECT je.value AS topic, COUNT(*) AS n,"
        " MIN(s.stated_at) AS first_at, MAX(s.stated_at) AS last_at"
        " FROM statement s, jsonb_array_elements_text(s.topics) je"
        " WHERE s.entity_id = %s"
        " GROUP BY je.value ORDER BY n DESC, topic ASC",
        (entity_id,))
    rows = await cur.fetchall()
    cur = await conn.execute(
        "SELECT topic, COUNT(*) AS n FROM position_shift"
        " WHERE entity_id = %s AND status = 'open' GROUP BY topic",
        (entity_id,))
    shift_counts = {r["topic"]: int(r["n"]) for r in await cur.fetchall()}
    out: list[EntityViewTopic] = []
    for row in rows:
        cur = await conn.execute(
            "SELECT s.position_summary FROM statement s"
            " WHERE s.entity_id = %s"
            " AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(s.topics)"
            "             je WHERE je.value = %s)"
            " ORDER BY s.stated_at DESC, s.id DESC LIMIT 1",
            (entity_id, row["topic"]))
        latest = await cur.fetchone()
        out.append(EntityViewTopic(
            topic=row["topic"], statement_count=int(row["n"]),
            first_at=row["first_at"], last_at=row["last_at"],
            shift_count=shift_counts.get(row["topic"], 0),
            latest_position=latest["position_summary"] if latest else None))
    return out


async def statements_for_topic(conn: psycopg.AsyncConnection,
                               entity_id: int,
                               topic: str) -> list[TopicStatement]:
    """The (entity, topic) statement timeline, newest first, with source
    display fields."""
    cur = await conn.execute(
        "SELECT s.id, s.quote, s.position_summary, s.stated_at,"
        " s.document_id, d.url, d.title,"
        " src.name AS source_name, src.credibility_tier"
        " FROM statement s"
        " JOIN document d ON d.id = s.document_id"
        " LEFT JOIN source src ON src.id = d.source_id"
        " WHERE s.entity_id = %s"
        " AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(s.topics) je"
        "             WHERE je.value = %s)"
        " ORDER BY s.stated_at DESC, s.id DESC",
        (entity_id, topic))
    return [
        TopicStatement(
            id=r["id"], quote=r["quote"],
            position_summary=r["position_summary"],
            stated_at=r["stated_at"], document_id=r["document_id"],
            source_name=r["source_name"],
            credibility_tier=r["credibility_tier"],
            url=r["url"], title=r["title"])
        for r in await cur.fetchall()]


async def shifts_for_topic(conn: psycopg.AsyncConnection, entity_id: int,
                           topic: str) -> list[PositionShiftRef]:
    """OPEN shifts for the (entity, topic) pair, newest first."""
    cur = await conn.execute(
        "SELECT id, kind, note, from_statement_id, to_statement_id,"
        " detected_at FROM position_shift"
        " WHERE entity_id = %s AND topic = %s AND status = 'open'"
        " ORDER BY detected_at DESC, id DESC", (entity_id, topic))
    return [
        PositionShiftRef(
            id=r["id"], kind=r["kind"], note=r["note"],
            from_statement_id=r["from_statement_id"],
            to_statement_id=r["to_statement_id"],
            detected_at=r["detected_at"])
        for r in await cur.fetchall()]


async def statements_for_document(conn: psycopg.AsyncConnection,
                                  document_id: int
                                  ) -> list[DocumentStatement]:
    cur = await conn.execute(
        "SELECT s.id, s.quote, s.topics, s.position_summary,"
        " e.id AS entity_id, e.name, e.entity_type"
        " FROM statement s JOIN entity e ON e.id = s.entity_id"
        " WHERE s.document_id = %s ORDER BY s.id", (document_id,))
    return [
        DocumentStatement(
            id=r["id"],
            speaker=EnrichmentEntityRef(
                id=r["entity_id"], name=r["name"],
                entity_type=r["entity_type"]),
            quote=r["quote"], topics=_topics(r["topics"]),
            position_summary=r["position_summary"])
        for r in await cur.fetchall()]


def _to_shift_row(row: Mapping[str, Any]) -> PositionShiftRow:
    return PositionShiftRow(
        id=row["id"], entity_id=row["entity_id"], topic=row["topic"],
        from_statement_id=row["from_statement_id"],
        to_statement_id=row["to_statement_id"], kind=row["kind"],
        note=row["note"], detected_at=row["detected_at"],
        status=row["status"])


async def dismiss_shift(conn: psycopg.AsyncConnection,
                        shift_id: int) -> PositionShiftRow | None:
    """Set status='dismissed'; returns the updated row (None if missing).
    Idempotent — dismissing twice keeps 'dismissed'."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE position_shift SET status = 'dismissed' WHERE id = %s",
            (shift_id,))
    if cur.rowcount == 0:
        return None
    cur = await conn.execute(
        "SELECT id, entity_id, topic, from_statement_id, to_statement_id,"
        " kind, note, detected_at, status FROM position_shift WHERE id = %s",
        (shift_id,))
    row = await cur.fetchone()
    return _to_shift_row(row) if row else None
