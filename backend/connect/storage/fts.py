"""Full-text query helpers — tsvector search over document.search_tsv
(and claim.search_tsv for the verification/reconciliation side).

v0.2: FTS5 MATCH + bm25() + snippet() become websearch_to_tsquery +
ts_rank_cd + ts_headline. ``websearch_to_tsquery`` accepts arbitrary user
input without ever raising, so the v0.1 ``safe_match`` quoted-phrase
fallback (and its OperationalError retry loop) is gone.

P3 (design §4): the ranking/count/hydration steps are exposed separately
(``rank_documents`` / ``count_documents`` / ``hydrate_page``) so the
retrieval layer can RRF-fuse the lexical ranking with the vector index and
hydrate the FUSED page — ``search_documents`` composes the same pieces for
the plain lexical surface.

XSS escape discipline is preserved byte-for-byte: ts_headline emits
control-char markers (\\x02/\\x03), the document text is HTML-escaped
AFTERWARDS, then the markers swap to <mark>/</mark> — raw document content
never reaches the client unescaped. Headlines are computed only over the
final result page (design §4), never inside the ranking query; a page doc
with zero lexical matches (found by the vector leg) gets the leading
fragment, which is acceptable v0.1-parity behavior for the snippet line.
"""

from __future__ import annotations

import html

import psycopg

from connect.domain.models import DocumentListItem
from connect.storage.documents import (  # shared row mapping + predicate
    _LIST_COLS,
    _to_list_item,
    VISIBLE_SQL,
)

# Control-char markers so we can HTML-escape the document text *after*
# ts_headline runs, then swap in the real <mark> tags — the client renders
# the snippet as HTML, so raw document content must never pass through
# unescaped.
_MARK_OPEN = "\x02"
_MARK_CLOSE = "\x03"
_HEADLINE_OPTS = (
    f"StartSel={_MARK_OPEN}, StopSel={_MARK_CLOSE},"
    " MaxFragments=2, MaxWords=24, MinWords=8, FragmentDelimiter=…")

_TSQUERY = "websearch_to_tsquery('english', %s)"


def _escape_snippet(snip: str) -> str:
    return (html.escape(snip, quote=False)
            .replace(_MARK_OPEN, "<mark>")
            .replace(_MARK_CLOSE, "</mark>"))


async def count_documents(conn: psycopg.AsyncConnection, q: str, *,
                          source_id: int | None = None,
                          viewer: int | None = None) -> int:
    """Corpus-wide lexical match count for the query. ``viewer`` (a user
    id) applies the tenancy visibility predicate; None = system view."""
    extra, params = "", [q]
    if source_id is not None:
        extra += " AND d.source_id = %s"
        params.append(source_id)
    if viewer is not None:
        extra += " AND " + VISIBLE_SQL
        params.append(viewer)
    cur = await conn.execute(
        f"SELECT COUNT(*) AS n FROM document d"
        f" WHERE d.search_tsv @@ {_TSQUERY}{extra}", params)
    return int((await cur.fetchone())["n"])


async def rank_documents(conn: psycopg.AsyncConnection, q: str, *,
                         limit: int, offset: int = 0,
                         source_id: int | None = None,
                         viewer: int | None = None) -> list[int]:
    """Cheap rank-ordered id query over the GIN index (no headlines)."""
    extra, params = "", [q]
    if source_id is not None:
        extra += " AND d.source_id = %s"
        params.append(source_id)
    if viewer is not None:
        extra += " AND " + VISIBLE_SQL
        params.append(viewer)
    cur = await conn.execute(
        f"SELECT d.id FROM document d"
        f" WHERE d.search_tsv @@ {_TSQUERY}{extra}"
        f" ORDER BY ts_rank_cd(d.search_tsv, {_TSQUERY}) DESC, d.id DESC"
        f" LIMIT %s OFFSET %s",
        (*params, q, limit, offset))
    return [r["id"] for r in await cur.fetchall()]


async def hydrate_page(conn: psycopg.AsyncConnection, q: str,
                       ids: list[int], *,
                       viewer: int | None = None) -> list[DocumentListItem]:
    """One final page of ids -> list items WITH escaped ts_headline
    snippets, preserving the given (rank/fusion) order. This is the only
    place ts_headline runs — it is expensive on long documents.

    The viewer predicate applies here too: the vector leg of hybrid search
    ranks over ALL embeddings, so a private doc id reaching this page must
    drop out rather than hydrate."""
    if not ids:
        return []
    extra, params = "", [q, _HEADLINE_OPTS, ids]
    if viewer is not None:
        extra = " AND " + VISIBLE_SQL
        params.append(viewer)
    cur = await conn.execute(
        f"SELECT {_LIST_COLS},"
        f" ts_headline('english', d.content_text, {_TSQUERY}, %s) AS snip"
        f" FROM document d LEFT JOIN source s ON s.id = d.source_id"
        f" WHERE d.id = ANY(%s){extra}",
        params)
    by_id = {r["id"]: r for r in await cur.fetchall()}
    return [_to_list_item(by_id[i], snippet=_escape_snippet(by_id[i]["snip"]))
            for i in ids if i in by_id]


async def search_documents(conn: psycopg.AsyncConnection, q: str, *,
                           page: int = 1, page_size: int = 20,
                           source_id: int | None = None,
                           viewer: int | None = None,
                           ) -> tuple[list[DocumentListItem], int]:
    """Rank-ordered full-text search returning list items with snippets.

    Two steps: a cheap ranking query over the GIN index picks the page of
    ids; the page is then hydrated WITH ts_headline (expensive on long
    documents — never run during ranking).
    """
    total = await count_documents(conn, q, source_id=source_id,
                                  viewer=viewer)
    page_ids = await rank_documents(conn, q, limit=page_size,
                                    offset=(page - 1) * page_size,
                                    source_id=source_id, viewer=viewer)
    return await hydrate_page(conn, q, page_ids, viewer=viewer), total


async def match_document(conn: psycopg.AsyncConnection, query: str,
                         doc_id: int) -> bool:
    """Does this single document match the search expression? (watch
    matching). websearch_to_tsquery tolerates any input — never raises."""
    cur = await conn.execute(
        f"SELECT 1 FROM document"
        f" WHERE id = %s AND search_tsv @@ {_TSQUERY}",
        (doc_id, query))
    return await cur.fetchone() is not None


async def search_claims(conn: psycopg.AsyncConnection, q: str, *,
                        limit: int = 20) -> list[tuple[int, str]]:
    """Ranked tsvector search over claim.search_tsv — (id, text)
    best-first. The claim-side counterpart of rank_documents (design §4)
    for verification/reconciliation candidate lookup; claim text is short
    and plain, so no headline pass."""
    cur = await conn.execute(
        f"SELECT id, text FROM claim"
        f" WHERE search_tsv @@ {_TSQUERY}"
        f" ORDER BY ts_rank_cd(search_tsv, {_TSQUERY}) DESC, id DESC"
        f" LIMIT %s",
        (q, q, limit))
    return [(int(r["id"]), r["text"]) for r in await cur.fetchall()]
