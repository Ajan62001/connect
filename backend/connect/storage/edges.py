"""Edge DAO — minimal Phase 0.5 surface.

edge.relation is free TEXT in the DDL; the controlled vocabulary lives in
domain/enums.EDGE_RELATIONS and is enforced HERE (adding a relation must
never require a table rebuild). Inserts are idempotent against the
active-edge unique index (OR IGNORE).
"""

from __future__ import annotations

import json
import sqlite3

from connect.domain import enums as E
from connect.storage.db import utc_now

# v8 causal vocabulary + node-type signatures (design §4): reaction_to /
# triggered_by connect events; enables/blocks span events and entities;
# alternative_to connects same-kind nodes (policy entity vs policy entity,
# event vs event). Enforced HERE — edge.relation stays free TEXT in DDL.
CAUSAL_RELATIONS: tuple[str, ...] = (
    "reaction_to", "triggered_by", "enables", "blocks", "alternative_to")

CAUSAL_SIGNATURES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "reaction_to": (("event",), ("event",)),
    "triggered_by": (("event",), ("event",)),
    "enables": (("event", "entity"), ("event", "entity")),
    "blocks": (("event", "entity"), ("event", "entity")),
    "alternative_to": (("entity", "event"), ("entity", "event")),
}


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


def insert_causal(conn: sqlite3.Connection, *, src_type: str, src_id: int,
                  dst_type: str, dst_id: int, relation: str,
                  properties: dict | None = None,
                  provenance_document_id: int | None = None,
                  provenance_dossier_id: int | None = None,
                  confidence: float | None = None,
                  grade: int = 2) -> int:
    """Insert one investigation-grade causal edge (idempotent against the
    active-edge unique index). Returns the edge id — the EXISTING active
    edge's id when an identical edge is already present, so findings always
    reference a real row. properties carries the grounding payload
    ({quote, speculation, finding_id, score_components?}).

    Raises ValueError on an off-vocabulary relation or a node-type pair
    outside the relation's signature.
    """
    sig = CAUSAL_SIGNATURES.get(relation)
    if sig is None:
        raise ValueError(f"unknown causal relation {relation!r}")
    src_ok, dst_ok = sig
    if src_type not in src_ok or dst_type not in dst_ok:
        raise ValueError(
            f"relation {relation!r} does not accept "
            f"{src_type!r} -> {dst_type!r}")
    if relation == "alternative_to" and src_type != dst_type:
        raise ValueError(
            "alternative_to connects same-kind nodes "
            f"({src_type!r} != {dst_type!r})")
    now = utc_now()
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO edge"
            " (src_type, src_id, dst_type, dst_id, relation, properties,"
            "  provenance_document_id, provenance_dossier_id, confidence,"
            "  grade, asserted_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (src_type, src_id, dst_type, dst_id, relation,
             json.dumps(properties or {}), provenance_document_id,
             provenance_dossier_id, confidence, grade, now, now))
    if cur.rowcount:
        return int(cur.lastrowid)  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT id FROM edge WHERE src_type = ? AND src_id = ?"
        " AND dst_type = ? AND dst_id = ? AND relation = ?"
        " AND status = 'active'",
        (src_type, src_id, dst_type, dst_id, relation)).fetchone()
    assert row is not None, "active edge vanished mid-insert"
    return int(row[0])


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
