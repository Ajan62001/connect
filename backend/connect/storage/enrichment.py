"""document_enrichment read DAO — assembles the API's Document.enrichment
field from the T1 row + its satellite tables."""

from __future__ import annotations

import sqlite3

from connect.domain.models import (
    DocumentEnrichment,
    EnrichmentClaimRef,
    EnrichmentEntityRef,
)


def get_for_document(conn: sqlite3.Connection,
                     document_id: int) -> DocumentEnrichment | None:
    row = conn.execute(
        "SELECT summary, event_type, model, created_at"
        " FROM document_enrichment WHERE document_id = ?",
        (document_id,)).fetchone()
    if row is None:
        return None
    topics = [r["topic"] for r in conn.execute(
        "SELECT topic FROM document_topic"
        " WHERE document_id = ? AND source = 't1' ORDER BY topic",
        (document_id,))]
    entities = [
        EnrichmentEntityRef(id=r["id"], name=r["name"],
                            entity_type=r["entity_type"])
        for r in conn.execute(
            "SELECT DISTINCT e.id, e.name, e.entity_type"
            " FROM entity_mention m JOIN entity e ON e.id = m.entity_id"
            " WHERE m.document_id = ? ORDER BY e.name", (document_id,))]
    claims = [
        EnrichmentClaimRef(id=r["id"], text=r["text"],
                           check_worthiness=r["check_worthiness"] or 0.0)
        for r in conn.execute(
            "SELECT c.id, c.text, c.check_worthiness"
            " FROM claim_sighting cs JOIN claim c ON c.id = cs.claim_id"
            " WHERE cs.document_id = ? ORDER BY c.id", (document_id,))]
    return DocumentEnrichment(
        summary=row["summary"], event_type=row["event_type"],
        topics=topics, entities=entities, claims=claims,
        model=row["model"], created_at=row["created_at"])
