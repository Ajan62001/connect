"""document_link DAO — raw SQL only; returns domain contracts.

The table doubles as the link-follow work queue (status machine:
not_followed -> pending -> fetched|failed) and as citation-graph lineage
(resolved_document_id mirrors the document-[links_to]->document edge).
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Protocol

from connect.domain.models import DocumentLink, LinkedFrom
from connect.storage.db import utc_now

_COLS = ("id, document_id, url, anchor_text, is_file, is_official, status,"
         " resolved_document_id, error, created_at")


class LinkFields(Protocol):
    """Anything carrying the four insert fields (ingestion.links.ClassifiedLink)."""
    url: str
    anchor_text: str | None
    is_file: bool
    is_official: bool


def _to_model(row: sqlite3.Row) -> DocumentLink:
    return DocumentLink(
        id=row["id"],
        document_id=row["document_id"],
        url=row["url"],
        anchor_text=row["anchor_text"],
        is_file=bool(row["is_file"]),
        is_official=bool(row["is_official"]),
        status=row["status"],
        resolved_document_id=row["resolved_document_id"],
        error=row["error"],
        created_at=row["created_at"],
    )


def insert_links(conn: sqlite3.Connection, document_id: int,
                 links: Iterable[LinkFields]) -> int:
    """Store extracted links; UNIQUE(document_id, url) makes re-extraction
    idempotent (OR IGNORE). Returns the number of NEW rows."""
    now = utc_now()
    inserted = 0
    with conn:
        for link in links:
            cur = conn.execute(
                "INSERT OR IGNORE INTO document_link"
                " (document_id, url, anchor_text, is_file, is_official,"
                "  created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (document_id, link.url, link.anchor_text,
                 int(link.is_file), int(link.is_official), now))
            inserted += cur.rowcount
    return inserted


def get(conn: sqlite3.Connection, link_id: int) -> DocumentLink | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM document_link WHERE id = ?", (link_id,)
    ).fetchone()
    return _to_model(row) if row else None


def list_for_document(conn: sqlite3.Connection,
                      document_id: int) -> list[DocumentLink]:
    """Extraction order (= insertion order = document order)."""
    rows = conn.execute(
        f"SELECT {_COLS} FROM document_link WHERE document_id = ?"
        " ORDER BY id", (document_id,)).fetchall()
    return [_to_model(r) for r in rows]


def has_links(conn: sqlite3.Connection, document_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM document_link WHERE document_id = ? LIMIT 1",
        (document_id,)).fetchone()
    return row is not None


def set_status(conn: sqlite3.Connection, link_id: int, status: str) -> None:
    with conn:
        conn.execute(
            "UPDATE document_link SET status = ? WHERE id = ?",
            (status, link_id))


def mark_fetched(conn: sqlite3.Connection, link_id: int,
                 resolved_document_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE document_link SET status = 'fetched',"
            " resolved_document_id = ?, error = NULL WHERE id = ?",
            (resolved_document_id, link_id))


def mark_failed(conn: sqlite3.Connection, link_id: int, error: str) -> None:
    with conn:
        conn.execute(
            "UPDATE document_link SET status = 'failed', error = ?"
            " WHERE id = ?", (error[:500], link_id))


def linked_from(conn: sqlite3.Connection,
                document_id: int) -> list[LinkedFrom]:
    """Parents whose extracted links resolved to this document."""
    rows = conn.execute(
        "SELECT DISTINCT l.document_id, d.title"
        " FROM document_link l JOIN document d ON d.id = l.document_id"
        " WHERE l.resolved_document_id = ? ORDER BY l.document_id",
        (document_id,)).fetchall()
    return [LinkedFrom(document_id=r["document_id"], title=r["title"])
            for r in rows]
