"""document_enrichment read DAO — assembles the API's Document.enrichment
field from the T1 row + its satellite tables."""

from __future__ import annotations

import psycopg

from connect.domain.models import (
    DocumentEnrichment,
    EnrichmentClaimRef,
    EnrichmentEntityRef,
)


async def get_for_document(conn: psycopg.AsyncConnection,
                           document_id: int) -> DocumentEnrichment | None:
    cur = await conn.execute(
        "SELECT summary, event_type, model, created_at"
        " FROM document_enrichment WHERE document_id = %s",
        (document_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    cur = await conn.execute(
        "SELECT topic FROM document_topic"
        " WHERE document_id = %s AND source = 't1' ORDER BY topic",
        (document_id,))
    topics = [r["topic"] for r in await cur.fetchall()]
    cur = await conn.execute(
        "SELECT DISTINCT e.id, e.name, e.entity_type"
        " FROM entity_mention m JOIN entity e ON e.id = m.entity_id"
        " WHERE m.document_id = %s ORDER BY e.name", (document_id,))
    entities = [
        EnrichmentEntityRef(id=r["id"], name=r["name"],
                            entity_type=r["entity_type"])
        for r in await cur.fetchall()]
    cur = await conn.execute(
        "SELECT c.id, c.text, c.check_worthiness"
        " FROM claim_sighting cs JOIN claim c ON c.id = cs.claim_id"
        " WHERE cs.document_id = %s ORDER BY c.id", (document_id,))
    claims = [
        EnrichmentClaimRef(id=r["id"], text=r["text"],
                           check_worthiness=r["check_worthiness"] or 0.0)
        for r in await cur.fetchall()]
    return DocumentEnrichment(
        summary=row["summary"], event_type=row["event_type"],
        topics=topics, entities=entities, claims=claims,
        model=row["model"], created_at=row["created_at"])
