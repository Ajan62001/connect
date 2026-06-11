"""document_link DAO — raw SQL only; returns domain contracts.

The table doubles as the link-follow work queue (status machine:
not_followed -> pending -> fetched|failed) and as citation-graph lineage
(resolved_document_id mirrors the document-[links_to]->document edge).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol

import psycopg

from connect.domain.models import DocumentLink, LinkedFrom
from connect.storage.pg import utc_now

_COLS = ("id, document_id, url, anchor_text, is_file, is_official, status,"
         " resolved_document_id, error, created_at")


class LinkFields(Protocol):
    """Anything carrying the four insert fields (ingestion.links.ClassifiedLink)."""
    url: str
    anchor_text: str | None
    is_file: bool
    is_official: bool


def _to_model(row: Mapping[str, Any]) -> DocumentLink:
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


async def insert_links(conn: psycopg.AsyncConnection, document_id: int,
                       links: Iterable[LinkFields]) -> int:
    """Store extracted links; UNIQUE(document_id, url) makes re-extraction
    idempotent (ON CONFLICT DO NOTHING). Returns the number of NEW rows."""
    now = utc_now()
    inserted = 0
    async with conn.transaction():
        for link in links:
            cur = await conn.execute(
                "INSERT INTO document_link"
                " (document_id, url, anchor_text, is_file, is_official,"
                "  created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (document_id, url) DO NOTHING",
                (document_id, link.url, link.anchor_text,
                 bool(link.is_file), bool(link.is_official), now))
            inserted += cur.rowcount
    return inserted


async def get(conn: psycopg.AsyncConnection,
              link_id: int) -> DocumentLink | None:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM document_link WHERE id = %s", (link_id,))
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def list_for_document(conn: psycopg.AsyncConnection,
                            document_id: int) -> list[DocumentLink]:
    """Extraction order (= insertion order = document order)."""
    cur = await conn.execute(
        f"SELECT {_COLS} FROM document_link WHERE document_id = %s"
        " ORDER BY id", (document_id,))
    return [_to_model(r) for r in await cur.fetchall()]


async def has_links(conn: psycopg.AsyncConnection,
                    document_id: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM document_link WHERE document_id = %s LIMIT 1",
        (document_id,))
    return await cur.fetchone() is not None


async def set_status(conn: psycopg.AsyncConnection, link_id: int,
                     status: str) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document_link SET status = %s WHERE id = %s",
            (status, link_id))


async def mark_fetched(conn: psycopg.AsyncConnection, link_id: int,
                       resolved_document_id: int) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document_link SET status = 'fetched',"
            " resolved_document_id = %s, error = NULL WHERE id = %s",
            (resolved_document_id, link_id))


async def mark_failed(conn: psycopg.AsyncConnection, link_id: int,
                      error: str) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document_link SET status = 'failed', error = %s"
            " WHERE id = %s", (error[:500], link_id))


async def linked_from(conn: psycopg.AsyncConnection,
                      document_id: int) -> list[LinkedFrom]:
    """Parents whose extracted links resolved to this document."""
    cur = await conn.execute(
        "SELECT DISTINCT l.document_id, d.title"
        " FROM document_link l JOIN document d ON d.id = l.document_id"
        " WHERE l.resolved_document_id = %s ORDER BY l.document_id",
        (document_id,))
    return [LinkedFrom(document_id=r["document_id"], title=r["title"])
            for r in await cur.fetchall()]
