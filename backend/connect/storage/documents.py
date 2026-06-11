"""Document aggregate DAO — raw SQL only; returns domain contracts."""

from __future__ import annotations

import sqlite3
from typing import Any

from connect.domain.models import Document, DocumentListItem

_LIST_COLS = """
d.id, d.source_id, s.name AS source_name, d.url, d.title, d.published_at,
d.fetched_at, d.media_type, d.enrichment_status, d.enrichment_tier,
d.watch_hit, d.canonical_document_id
"""

_FULL_COLS = _LIST_COLS + """,
d.author, d.language, d.content_text, d.content_hash
"""

_FROM = " FROM document d LEFT JOIN source s ON s.id = d.source_id "


def _to_list_item(row: sqlite3.Row, snippet: str | None = None) -> DocumentListItem:
    return DocumentListItem(
        id=row["id"],
        source_id=row["source_id"],
        source_name=row["source_name"],
        url=row["url"],
        title=row["title"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        media_type=row["media_type"],
        enrichment_status=row["enrichment_status"],
        enrichment_tier=row["enrichment_tier"],
        watch_hit=bool(row["watch_hit"]),
        canonical_document_id=row["canonical_document_id"],
        snippet=snippet,
    )


def _to_document(row: sqlite3.Row) -> Document:
    base = _to_list_item(row).model_dump()
    base.update(
        author=row["author"],
        language=row["language"],
        content_text=row["content_text"],
        content_hash=row["content_hash"],
    )
    return Document(**base)


def insert(conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
    """Insert a document row; caller supplies the already-normalized fields.
    The FTS external-content triggers index it automatically."""
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with conn:
        cur = conn.execute(
            f"INSERT INTO document ({cols}) VALUES ({marks})",
            tuple(fields.values()))
    return int(cur.lastrowid)  # type: ignore[arg-type]


def get(conn: sqlite3.Connection, doc_id: int) -> Document | None:
    row = conn.execute(
        "SELECT " + _FULL_COLS + _FROM + " WHERE d.id = ?", (doc_id,)).fetchone()
    return _to_document(row) if row else None


def get_by_hash(conn: sqlite3.Connection, content_hash: str) -> Document | None:
    row = conn.execute(
        "SELECT " + _FULL_COLS + _FROM + " WHERE d.content_hash = ?",
        (content_hash,)).fetchone()
    return _to_document(row) if row else None


def get_by_url(conn: sqlite3.Connection, url: str) -> Document | None:
    """Exact match on either the requested or the post-redirect URL."""
    row = conn.execute(
        "SELECT " + _FULL_COLS + _FROM +
        " WHERE d.url = ? OR d.canonical_url = ? LIMIT 1",
        (url, url)).fetchone()
    return _to_document(row) if row else None


def url_exists(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM document WHERE url = ? LIMIT 1", (url,)).fetchone()
    return row is not None


def set_blob_path(conn: sqlite3.Connection, doc_id: int, path: str) -> None:
    with conn:
        conn.execute(
            "UPDATE document SET raw_blob_path = ? WHERE id = ?", (path, doc_id))


def set_watch_hit(conn: sqlite3.Connection, doc_id: int) -> None:
    with conn:
        conn.execute("UPDATE document SET watch_hit = 1 WHERE id = ?", (doc_id,))


def recent_simhashes(conn: sqlite3.Connection,
                     since_iso: str) -> list[tuple[int, int, int | None]]:
    """(id, simhash, canonical_document_id) for the near-dup window scan."""
    rows = conn.execute(
        "SELECT id, simhash, canonical_document_id FROM document"
        " WHERE simhash IS NOT NULL AND fetched_at >= ?", (since_iso,)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def list_page(conn: sqlite3.Connection, *, page: int, page_size: int,
              source_id: int | None = None,
              enrichment_status: str | None = None,
              ) -> tuple[list[DocumentListItem], int]:
    """Newest-first listing (the /feed and unfiltered /documents view)."""
    where, params = [], []
    if source_id is not None:
        where.append("d.source_id = ?")
        params.append(source_id)
    if enrichment_status is not None:
        where.append("d.enrichment_status = ?")
        params.append(enrichment_status)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        "SELECT COUNT(*)" + _FROM + where_sql, params).fetchone()[0]
    rows = conn.execute(
        "SELECT " + _LIST_COLS + _FROM + where_sql +
        " ORDER BY d.fetched_at DESC, d.id DESC LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size)).fetchall()
    return [_to_list_item(r) for r in rows], int(total)
