"""Row factories shared by the Phase 2 tests (clustering, threading,
promotion, briefing). Synthetic embeddings are injected directly into
document_embedding — no model downloads ever."""

from __future__ import annotations

import sqlite3
from itertools import count

from connect.knowledge.vector import _pack
from connect.storage.db import utc_now

_seq = count(1)


def insert_source(conn: sqlite3.Connection, name: str, *, tier: int = 2,
                  notes: str | None = None, config: str = "{}") -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO source (name, type, config, credibility_tier,"
            " notes, created_at) VALUES (?, 'manual', ?, ?, ?, ?)",
            (name, config, tier, notes, utc_now()))
    return int(cur.lastrowid)


def insert_doc(conn: sqlite3.Connection, *, title: str = "Doc",
               text: str = "body text", source_id: int | None = None,
               published_at: str | None = None, watch_hit: int = 0) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO document (source_id, title, published_at,"
            " fetched_at, media_type, content_text, content_hash,"
            " enrichment_status, watch_hit)"
            " VALUES (?,?,?,?, 'text', ?, ?, 'pending', ?)",
            (source_id, title, published_at, utc_now(), text,
             f"h-{next(_seq)}", watch_hit))
    return int(cur.lastrowid)


def add_t1(conn: sqlite3.Connection, doc_id: int, *,
           summary: str = "One-line summary.", event_type: str = "other",
           model: str = "claude-haiku-4-5") -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO document_enrichment (document_id,"
            " summary, event_type, model, prompt_version, created_at)"
            " VALUES (?,?,?,?, 't1-v1', ?)",
            (doc_id, summary, event_type, model, utc_now()))
        conn.execute(
            "UPDATE document SET enrichment_status='done',"
            " enrichment_tier = MAX(enrichment_tier, 1) WHERE id = ?",
            (doc_id,))


def entity_id(conn: sqlite3.Connection, name: str,
              etype: str = "organization") -> int:
    row = conn.execute(
        "SELECT id FROM entity WHERE name = ? AND entity_type = ?",
        (name, etype)).fetchone()
    if row is not None:
        return int(row[0])
    with conn:
        cur = conn.execute(
            "INSERT INTO entity (name, entity_type, aliases, created_at)"
            " VALUES (?, ?, '[]', ?)", (name, etype, utc_now()))
    return int(cur.lastrowid)


def add_mentions(conn: sqlite3.Connection, doc_id: int,
                 names: tuple[str, ...]) -> list[int]:
    ids = []
    with conn:
        for name in names:
            eid = entity_id(conn, name)
            conn.execute(
                "INSERT INTO entity_mention (document_id, entity_id,"
                " surface, method, created_at) VALUES (?,?,?, 'llm', ?)",
                (doc_id, eid, name, utc_now()))
            ids.append(eid)
    return ids


def set_vector(conn: sqlite3.Connection, doc_id: int,
               vec: list[float]) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO document_embedding (document_id, model,"
            " dim, vector) VALUES (?, 'test', ?, ?)",
            (doc_id, len(vec), _pack(vec)))


def t1_doc(conn: sqlite3.Connection, *, title: str = "Doc",
           summary: str = "One-line summary.", event_type: str = "other",
           entities: tuple[str, ...] = (), vec: list[float] | None = None,
           source_id: int | None = None, published_at: str | None = None,
           watch_hit: int = 0) -> int:
    """One T1-enriched document with mentions and an injected embedding —
    the exact input shape T2 expects."""
    doc_id = insert_doc(conn, title=title, source_id=source_id,
                        published_at=published_at, watch_hit=watch_hit)
    add_t1(conn, doc_id, summary=summary, event_type=event_type)
    if entities:
        add_mentions(conn, doc_id, entities)
    if vec is not None:
        set_vector(conn, doc_id, vec)
    return doc_id
