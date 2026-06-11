"""FTS5 query helpers — search over the document_fts external-content table.

User queries can contain FTS5 syntax errors; ``safe_match`` falls back to a
quoted phrase so a stray `"` or `-` never 500s a search.
"""

from __future__ import annotations

import html
import sqlite3

from connect.domain.models import DocumentListItem
from connect.storage.documents import _LIST_COLS, _to_list_item  # shared row mapping

# Control-char markers so we can HTML-escape the document text *after*
# snippet() runs, then swap in the real <mark> tags — the client renders the
# snippet as HTML, so raw document content must never pass through unescaped.
_MARK_OPEN = "\x02"
_MARK_CLOSE = "\x03"
_SNIPPET = f"snippet(document_fts, 1, '{_MARK_OPEN}', '{_MARK_CLOSE}', '…', 24)"


def _escape_snippet(snip: str) -> str:
    return (html.escape(snip, quote=False)
            .replace(_MARK_OPEN, "<mark>")
            .replace(_MARK_CLOSE, "</mark>"))


def _phrase(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'


def search_documents(conn: sqlite3.Connection, q: str, *,
                     page: int = 1, page_size: int = 20,
                     source_id: int | None = None,
                     ) -> tuple[list[DocumentListItem], int]:
    """BM25-ranked FTS search returning list items with snippets."""
    for attempt, match in enumerate((q, _phrase(q))):
        try:
            return _search(conn, match, page=page, page_size=page_size,
                           source_id=source_id)
        except sqlite3.OperationalError:
            if attempt == 1:
                raise
    return [], 0  # unreachable


def _search(conn: sqlite3.Connection, match: str, *, page: int,
            page_size: int, source_id: int | None,
            ) -> tuple[list[DocumentListItem], int]:
    where = "WHERE document_fts MATCH ?"
    params: list = [match]
    if source_id is not None:
        where += " AND d.source_id = ?"
        params.append(source_id)
    total = conn.execute(
        f"SELECT COUNT(*) FROM document_fts"
        f" JOIN document d ON d.id = document_fts.rowid"
        f" LEFT JOIN source s ON s.id = d.source_id {where}",
        params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_LIST_COLS}, {_SNIPPET} AS snip"
        f" FROM document_fts"
        f" JOIN document d ON d.id = document_fts.rowid"
        f" LEFT JOIN source s ON s.id = d.source_id"
        f" {where} ORDER BY bm25(document_fts) LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size)).fetchall()
    return [_to_list_item(r, snippet=_escape_snippet(r["snip"]))
            for r in rows], int(total)


def match_document(conn: sqlite3.Connection, query: str, doc_id: int) -> bool:
    """Does this single document match the FTS expression? (watch matching).
    A malformed watch query simply doesn't match — never raises."""
    for match in (query, _phrase(query)):
        try:
            row = conn.execute(
                "SELECT 1 FROM document_fts WHERE document_fts MATCH ?"
                " AND rowid = ?", (match, doc_id)).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            continue
    return False
