"""Deterministic, LLM-free watch matching (T0 hook) + thin CRUD pass-through.

match_document(doc_id) runs after every ingest:
- topic/search watches: one FTS5 MATCH per watch, constrained to the new
  document's rowid;
- entity watches: word-boundary match of the entity's canonical name + every
  alias against title+content (case-insensitive). This gets better for free
  as the alias table grows.

Hits insert watch_hit rows and flip document.watch_hit — the flag the
enrichment scheduler (Phase 1) fast-paths on.
"""

from __future__ import annotations

import json
import re
import sqlite3

from connect.domain.models import Watch
from connect.storage import fts as fts_dao
from connect.storage import watches as watch_dao


def _entity_terms(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    row = conn.execute(
        "SELECT name, aliases FROM entity WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return []
    terms = [row["name"]]
    try:
        terms.extend(a for a in json.loads(row["aliases"] or "[]")
                     if isinstance(a, str))
    except (ValueError, TypeError):
        pass
    return [t for t in terms if t.strip()]


def _word_boundary_match(terms: list[str], text: str) -> bool:
    for term in terms:
        if re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE):
            return True
    return False


def match_document(conn: sqlite3.Connection, doc_id: int) -> list[int]:
    """Match one freshly-ingested document against all unmuted watches.
    Returns the ids of watches that hit."""
    row = conn.execute(
        "SELECT title, content_text FROM document WHERE id = ?",
        (doc_id,)).fetchone()
    if row is None:
        return []
    text = (row["title"] or "") + "\n" + (row["content_text"] or "")

    hits: list[int] = []
    for watch in watch_dao.list_active(conn):
        matched = False
        if watch.kind in ("topic", "search") and watch.query_fts:
            matched = fts_dao.match_document(conn, watch.query_fts, doc_id)
        elif watch.kind == "entity" and watch.entity_id is not None:
            terms = _entity_terms(conn, watch.entity_id)
            matched = _word_boundary_match(terms, text) if terms else False
        # 'thread'/'claim' watches hit on events/claims, not raw documents —
        # those object types arrive in later phases.
        if matched:
            watch_dao.insert_hit(conn, watch.id, "document", doc_id)
            hits.append(watch.id)
    if hits:
        from connect.storage import documents as doc_dao
        doc_dao.set_watch_hit(conn, doc_id)
    return hits


def badges(conn: sqlite3.Connection) -> dict[int, int]:
    return watch_dao.badges(conn)


def mark_seen(conn: sqlite3.Connection, watch_id: int) -> Watch | None:
    return watch_dao.mark_seen(conn, watch_id)
