"""T2 — graph integration for promoted documents (Phase 2 slice).

Phase 2 scope: event-cluster assignment + story threading for new events.
(Full T2 relation extraction / claim reconciliation arrives in Phase 3.)

Runs after T1 inside the sweep for documents whose promotion triggers fired,
and as a standalone 'enrich_t2' job for the manual promote endpoint. LLM
usage inside is governor-checked per call by the clusterer/threader; the
deterministic paths are free, so T2 never blocks on budget — it degrades.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from connect.knowledge.linking import event_clusterer, story_threader
from connect.llm.provider import LLMProvider
from connect.llm.spend import Governor

log = logging.getLogger(__name__)


def already_assigned(conn: sqlite3.Connection, document_id: int) -> int | None:
    """The event this doc is already clustered into, if any (idempotence)."""
    row = conn.execute(
        "SELECT event_id FROM event_assignment"
        " WHERE document_id = ? AND event_id IS NOT NULL"
        " ORDER BY id DESC LIMIT 1", (document_id,)).fetchone()
    return int(row[0]) if row else None


async def process_document(conn: sqlite3.Connection,
                            provider: LLMProvider | None,
                            governor: Governor, document_id: int, *,
                            triggers: list[str]) -> dict[str, Any]:
    """Event assignment (+ threading for new events) for one T1-enriched
    document. Idempotent: an already-assigned doc is left alone."""
    stats: dict[str, Any] = {"document_id": document_id, "triggers": triggers}
    existing = already_assigned(conn, document_id)
    if existing is not None:
        stats["skipped"] = "already_assigned"
        stats["event_id"] = existing
        return stats

    result = await event_clusterer.assign_document(
        conn, provider, governor, document_id)
    if result is None:
        stats["skipped"] = "not_t1_enriched"
        return stats
    stats.update(event_id=result.event_id, method=result.method,
                 score=result.score, created_event=result.created_event,
                 assignment_id=result.assignment_id)

    if result.created_event:
        threading = await story_threader.thread_new_event(
            conn, provider, governor, result.event_id,
            provenance_document_id=document_id)
        stats["threading"] = threading
    else:
        # attaching to an event inside a story moves the thread
        row = conn.execute("SELECT story_id FROM event WHERE id = ?",
                           (result.event_id,)).fetchone()
        if row is not None and row["story_id"] is not None:
            story_threader.refresh_story(conn, row["story_id"])
            stats["story_id"] = row["story_id"]

    with conn:
        conn.execute(
            "UPDATE document SET enrichment_tier = MAX(enrichment_tier, 2)"
            " WHERE id = ?", (document_id,))
    return stats
