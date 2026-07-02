"""Content-item provenance DAO — the ``content_item_source`` link table.

S1 of the editorial-integrity suite. Every content_item is tied to the specific
source documents (verbatim quote + char offsets + a credibility-tier snapshot)
it was grounded in. These rows are dual-written alongside the legacy JSONB
``content_item.sources`` inside the SAME ``content_items.insert`` transaction, so
the two provenance stores can never diverge. The relational rows add referential
integrity and the reverse index (``list_for_document``) that the verification
gate (S2), corrections propagation (S3), credibility scoring (S4), and the
reader-facing trust panel (S6) all read.

Note (over-attribution caveat): ``content/ground.resolve`` falls back to the
WHOLE evidence menu when the model cited nothing valid, so a link row means
"this source was on the menu for this item", NOT "this specific sentence is
backed by this source". Downstream consumers must not treat a link row as a
verified per-claim backing — that semantic guarantee is what S2's gate adds.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import psycopg

from connect.storage.pg import utc_now

_LINK_COLS = (
    "id, content_item_id, ref, document_id, finding_id, source_name, title,"
    " url, quote, quote_start, quote_end, occurred_on, credibility_tier,"
    " stance, verdict, created_at")

# 13 bound params; credibility_tier is resolved by a correlated subquery from
# the cited document's registered source so the tier is snapshotted at write.
_INSERT = (
    "INSERT INTO content_item_source"
    " (content_item_id, ref, document_id, finding_id, source_name, title, url,"
    "  quote, quote_start, quote_end, occurred_on, credibility_tier,"
    "  created_at)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
    "         (SELECT src.credibility_tier FROM source src"
    "            JOIN document d ON d.source_id = src.id WHERE d.id = %s),"
    "         %s)")


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


async def insert_links(conn: psycopg.AsyncConnection, content_item_id: int,
                       sources: Sequence[Mapping[str, Any]]) -> int:
    """Fan a list of StorySource-shaped dicts into ``content_item_source`` rows.

    MUST run inside the caller's transaction (``content_items.insert``). Returns
    the number of link rows written. Source lists are bounded by the evidence
    menu size (typically < 20), so a simple per-row INSERT is used rather than
    executemany — it keeps the credibility-tier subquery readable.
    """
    n = 0
    now = utc_now()
    for s in sources:
        if not isinstance(s, dict):
            continue
        doc_id = _int_or_none(s.get("document_id"))
        await conn.execute(_INSERT, (
            content_item_id, str(s.get("ref") or ""), doc_id,
            _int_or_none(s.get("finding_id")), s.get("source_name"),
            s.get("title"), s.get("url"), s.get("quote"),
            _int_or_none(s.get("quote_start")), _int_or_none(s.get("quote_end")),
            s.get("occurred_on"), doc_id, now))
        n += 1
    return n


async def list_for_item(conn: psycopg.AsyncConnection,
                        content_item_id: int) -> list[dict[str, Any]]:
    """All provenance link rows for one content_item (verifier / trust panel)."""
    cur = await conn.execute(
        f"SELECT {_LINK_COLS} FROM content_item_source"
        " WHERE content_item_id = %s ORDER BY id", (content_item_id,))
    return [dict(r) for r in await cur.fetchall()]


async def list_for_document(conn: psycopg.AsyncConnection,
                            document_id: int) -> list[dict[str, Any]]:
    """Reverse index: every content_item grounded in a given document.

    The substrate for corrections/retraction propagation (S3) and dynamic
    credibility (S4) — when a document is retracted or its claim's verdict
    flips, this finds the published items that depend on it.
    """
    cur = await conn.execute(
        f"SELECT {_LINK_COLS} FROM content_item_source"
        " WHERE document_id = %s ORDER BY content_item_id", (document_id,))
    return [dict(r) for r in await cur.fetchall()]
