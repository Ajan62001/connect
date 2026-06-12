"""Document aggregate DAO — raw SQL only; returns domain contracts.

v0.2 Phase C (tenancy design §1): the read surfaces carry the
defense-in-depth visibility predicate ``(d.visibility = 'shared' OR
d.owner_id = :viewer)``. ``viewer`` follows the design's DAO convention —
``int`` is a user id, ``None`` is the SYSTEM viewer (workers/pipeline
internals, no predicate). API routers always pass the requester's id.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import Document, DocumentListItem

_LIST_COLS = """
d.id, d.source_id, s.name AS source_name, d.url, d.title, d.published_at,
d.fetched_at, d.media_type, d.enrichment_status, d.enrichment_tier,
d.watch_hit, d.canonical_document_id, d.owner_id, d.visibility, d.origin
"""

_FULL_COLS = _LIST_COLS + """,
d.author, d.language, d.content_text, d.content_hash
"""

_FROM = " FROM document d LEFT JOIN source s ON s.id = d.source_id "

# the tenancy read predicate (one %s: the viewer's user id)
VISIBLE_SQL = "(d.visibility = 'shared' OR d.owner_id = %s)"


def _to_list_item(row: Mapping[str, Any],
                  snippet: str | None = None) -> DocumentListItem:
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
        owner_id=row["owner_id"],
        visibility=row["visibility"],
        origin=row["origin"],
    )


def _to_document(row: Mapping[str, Any]) -> Document:
    base = _to_list_item(row).model_dump()
    base.update(
        author=row["author"],
        language=row["language"],
        content_text=row["content_text"],
        content_hash=row["content_hash"],
    )
    return Document(**base)


async def insert(conn: psycopg.AsyncConnection,
                 fields: dict[str, Any]) -> int:
    """Insert a document row; caller supplies the already-normalized fields.
    The generated search_tsv column indexes it automatically."""
    cols = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    async with conn.transaction():
        cur = await conn.execute(
            f"INSERT INTO document ({cols}) VALUES ({marks}) RETURNING id",
            tuple(fields.values()))
        row = await cur.fetchone()
    return int(row["id"])


async def get(conn: psycopg.AsyncConnection, doc_id: int, *,
              viewer: int | None = None) -> Document | None:
    """The full document; with a ``viewer`` the visibility predicate
    applies (another user's private doc reads as absent -> 404)."""
    sql = "SELECT " + _FULL_COLS + _FROM + " WHERE d.id = %s"
    params: list[Any] = [doc_id]
    if viewer is not None:
        sql += " AND " + VISIBLE_SQL
        params.append(viewer)
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return _to_document(row) if row else None


async def get_by_hash(conn: psycopg.AsyncConnection,
                      content_hash: str) -> Document | None:
    cur = await conn.execute(
        "SELECT " + _FULL_COLS + _FROM + " WHERE d.content_hash = %s",
        (content_hash,))
    row = await cur.fetchone()
    return _to_document(row) if row else None


async def get_by_url(conn: psycopg.AsyncConnection,
                     url: str) -> Document | None:
    """Exact match on either the requested or the post-redirect URL."""
    cur = await conn.execute(
        "SELECT " + _FULL_COLS + _FROM +
        " WHERE d.url = %s OR d.canonical_url = %s LIMIT 1",
        (url, url))
    row = await cur.fetchone()
    return _to_document(row) if row else None


async def url_exists(conn: psycopg.AsyncConnection, url: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM document WHERE url = %s LIMIT 1", (url,))
    return await cur.fetchone() is not None


async def set_blob_path(conn: psycopg.AsyncConnection, doc_id: int,
                        path: str) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document SET raw_blob_path = %s WHERE id = %s",
            (path, doc_id))


async def set_watch_hit(conn: psycopg.AsyncConnection, doc_id: int) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document SET watch_hit = TRUE WHERE id = %s", (doc_id,))


async def visibility_of(conn: psycopg.AsyncConnection,
                        doc_id: int) -> tuple[str, int | None] | None:
    """(visibility, owner_id) — the write-gate lookup; None when absent."""
    cur = await conn.execute(
        "SELECT visibility, owner_id FROM document WHERE id = %s",
        (doc_id,))
    row = await cur.fetchone()
    return (row["visibility"], row["owner_id"]) if row else None


async def set_visibility(conn: psycopg.AsyncConnection, doc_id: int,
                         visibility: str) -> None:
    """Flip a document's visibility. Sharing requires no special action —
    the next enrichment sweep picks the now-eligible doc up (design §1)."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE document SET visibility = %s WHERE id = %s",
            (visibility, doc_id))


async def has_shared_derivatives(conn: psycopg.AsyncConnection,
                                 doc_id: int) -> bool:
    """Has this document already compounded into the shared KB? (mentions,
    sightings, statements, evidence, enrichment, event assignments, edge
    provenance) — guards the shared->private flip, which would violate I1
    retroactively."""
    checks = (
        "SELECT 1 FROM document_enrichment WHERE document_id = %s",
        "SELECT 1 FROM entity_mention WHERE document_id = %s",
        "SELECT 1 FROM claim_sighting WHERE document_id = %s",
        "SELECT 1 FROM statement WHERE document_id = %s",
        "SELECT 1 FROM evidence WHERE document_id = %s",
        "SELECT 1 FROM event_assignment WHERE document_id = %s",
        "SELECT 1 FROM edge WHERE provenance_document_id = %s",
    )
    for sql in checks:
        cur = await conn.execute(sql + " LIMIT 1", (doc_id,))
        if await cur.fetchone() is not None:
            return True
    return False


async def recent_simhashes(
        conn: psycopg.AsyncConnection,
        since_iso: str, *,
        owner_id: int | None = None) -> list[tuple[int, int, int | None]]:
    """(id, simhash, canonical_document_id) for the near-dup window scan.

    Tenancy: a new doc must never be canonicalized onto a private doc its
    ingester cannot see — candidates are shared docs plus (when the ingest
    has an owner) that owner's own private docs.
    """
    sql = ("SELECT id, simhash, canonical_document_id FROM document"
           " WHERE simhash IS NOT NULL AND fetched_at >= %s")
    params: list[Any] = [since_iso]
    if owner_id is None:
        sql += " AND visibility = 'shared'"
    else:
        sql += " AND (visibility = 'shared' OR owner_id = %s)"
        params.append(owner_id)
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    return [(r["id"], r["simhash"], r["canonical_document_id"])
            for r in rows]


async def set_workspace(conn: psycopg.AsyncConnection, doc_id: int,
                        workspace_id: int | None) -> None:
    """Tag (or untag) a document into a workspace's knowledge base."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE document SET workspace_id = %s WHERE id = %s",
            (workspace_id, doc_id))


async def list_in_workspace(conn: psycopg.AsyncConnection, workspace_id: int,
                            *, viewer: int | None = None, page: int = 1,
                            page_size: int = 20,
                            ) -> tuple[list[DocumentListItem], int]:
    """A workspace's own KB documents, newest first, viewer-scoped."""
    where = ["d.workspace_id = %s"]
    params: list = [workspace_id]
    if viewer is not None:
        where.append(VISIBLE_SQL)
        params.append(viewer)
    where_sql = " WHERE " + " AND ".join(where)
    cur = await conn.execute("SELECT COUNT(*) AS n" + _FROM + where_sql, params)
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT " + _LIST_COLS + _FROM + where_sql
        + " ORDER BY d.fetched_at DESC, d.id DESC LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size))
    return [_to_list_item(r) for r in await cur.fetchall()], int(total)


async def list_page(conn: psycopg.AsyncConnection, *, page: int,
                    page_size: int, source_id: int | None = None,
                    enrichment_status: str | None = None,
                    viewer: int | None = None,
                    ) -> tuple[list[DocumentListItem], int]:
    """Newest-first listing (the /feed and unfiltered /documents view)."""
    where, params = [], []
    if source_id is not None:
        where.append("d.source_id = %s")
        params.append(source_id)
    if enrichment_status is not None:
        where.append("d.enrichment_status = %s")
        params.append(enrichment_status)
    if viewer is not None:
        where.append(VISIBLE_SQL)
        params.append(viewer)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    cur = await conn.execute(
        "SELECT COUNT(*) AS n" + _FROM + where_sql, params)
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT " + _LIST_COLS + _FROM + where_sql +
        " ORDER BY d.fetched_at DESC, d.id DESC LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    return [_to_list_item(r) for r in rows], int(total)
