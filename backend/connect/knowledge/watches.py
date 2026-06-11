"""Deterministic, LLM-free watch matching (T0 hook) + thin CRUD pass-through.

match_document(doc_id) runs after every ingest:
- topic/search watches: one tsquery match per watch, constrained to the new
  document's id;
- entity watches: word-boundary match of the entity's canonical name + every
  alias against title+content (case-insensitive). This gets better for free
  as the alias table grows.

Hits insert watch_hit rows and flip document.watch_hit — the flag the
enrichment scheduler (Phase 1) fast-paths on.
"""

from __future__ import annotations

import re

import psycopg

from connect.domain.models import Watch
from connect.storage import fts as fts_dao
from connect.storage import watches as watch_dao


async def _entity_terms(conn: psycopg.AsyncConnection,
                        entity_id: int) -> list[str]:
    cur = await conn.execute(
        "SELECT name, aliases FROM entity WHERE id = %s", (entity_id,))
    row = await cur.fetchone()
    if row is None:
        return []
    terms = [row["name"]]
    terms.extend(a for a in (row["aliases"] or []) if isinstance(a, str))
    return [t for t in terms if t.strip()]


def _word_boundary_match(terms: list[str], text: str) -> bool:
    for term in terms:
        if re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE):
            return True
    return False


async def match_document(conn: psycopg.AsyncConnection,
                         doc_id: int) -> list[int]:
    """Match one freshly-ingested document against all unmuted watches.
    Returns the ids of watches that hit."""
    cur = await conn.execute(
        "SELECT title, content_text FROM document WHERE id = %s",
        (doc_id,))
    row = await cur.fetchone()
    if row is None:
        return []
    text = (row["title"] or "") + "\n" + (row["content_text"] or "")

    hits: list[int] = []
    for watch in await watch_dao.list_active(conn):
        matched = False
        if watch.kind in ("topic", "search") and watch.query_fts:
            matched = await fts_dao.match_document(
                conn, watch.query_fts, doc_id)
        elif watch.kind == "entity" and watch.entity_id is not None:
            terms = await _entity_terms(conn, watch.entity_id)
            matched = _word_boundary_match(terms, text) if terms else False
        # 'thread'/'claim' watches hit on events/claims, not raw documents —
        # those object types arrive in later phases.
        if matched:
            await watch_dao.insert_hit(conn, watch.id, "document", doc_id)
            hits.append(watch.id)
    if hits:
        from connect.storage import documents as doc_dao
        await doc_dao.set_watch_hit(conn, doc_id)
    return hits


async def badges(conn: psycopg.AsyncConnection) -> dict[int, int]:
    return await watch_dao.badges(conn)


async def mark_seen(conn: psycopg.AsyncConnection,
                    watch_id: int) -> Watch | None:
    return await watch_dao.mark_seen(conn, watch_id)
