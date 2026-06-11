"""Edge DAO — minimal Phase 0.5 surface.

edge.relation is free TEXT in the DDL; the controlled vocabulary lives in
domain/enums.EDGE_RELATIONS and is enforced HERE (adding a relation must
never require a table rebuild). Inserts are idempotent against the
active-edge unique index (OR IGNORE).
"""

from __future__ import annotations

import sqlite3

from connect.domain import enums as E
from connect.storage.db import utc_now


def insert(conn: sqlite3.Connection, *, src_type: str, src_id: int,
           dst_type: str, dst_id: int, relation: str,
           provenance_document_id: int | None = None,
           grade: int = 1) -> int | None:
    """Insert an active edge; returns the new edge id, or None when an
    identical active edge already exists (idx_edge_active_unique)."""
    if relation not in E.EDGE_RELATIONS:
        raise ValueError(f"unknown edge relation {relation!r}")
    if src_type not in E.NODE_TYPES or dst_type not in E.NODE_TYPES:
        raise ValueError(f"unknown node type {src_type!r}/{dst_type!r}")
    now = utc_now()
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO edge"
            " (src_type, src_id, dst_type, dst_id, relation,"
            "  provenance_document_id, grade, asserted_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (src_type, src_id, dst_type, dst_id, relation,
             provenance_document_id, grade, now, now))
    return int(cur.lastrowid) if cur.rowcount else None


def insert_links_to(conn: sqlite3.Connection, parent_document_id: int,
                    child_document_id: int) -> int | None:
    """document(parent) -[links_to]-> document(child); provenance = parent."""
    return insert(
        conn,
        src_type="document", src_id=parent_document_id,
        dst_type="document", dst_id=child_document_id,
        relation="links_to",
        provenance_document_id=parent_document_id,
        grade=1)
