"""Dossier visibility + the share cascade (tenancy design §1).

``private -> shared`` requires every document cited by the dossier's
findings/evidence to be shared. Cited private docs the OWNER owns are
auto-shared once the caller confirms (the API answers 409 with the
confirmation list until then — the UI's confirmation dialog); a cited
private doc belonging to someone else is impossible by write gate 3 but
checked defensively (409). After the doc shares, the deferred grade-2
edges persisted in ``finding.payload["link"]`` are materialized into the
shared graph.

``shared -> private`` is NEVER allowed (409): a shared dossier's writeback
has already compounded into the shared KB (edges/claims/evidence); the
docs it auto-shared may have been enriched — retracting would tear the
graph and violate I1 retroactively.

Used by both PATCH /api/analyses/{id} and /api/investigations/{id}; the
caller (router) owns the owner-or-admin authorization check.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import DossierVisibilityResult, ShareDocumentRef
from connect.investigation.writeback import _edge_properties
from connect.storage import documents as doc_dao
from connect.storage import edges as edge_dao


class VisibilityChangeRejected(Exception):
    """A 409-shaped rejection; ``reason`` is machine-readable and
    ``documents`` carries the confirmation list when applicable."""

    def __init__(self, reason: str, detail: str,
                 documents: list[ShareDocumentRef] | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.documents = documents or []


async def cited_documents(conn: psycopg.AsyncConnection,
                          dossier_id: int) -> set[int]:
    """Every document id the dossier's findings/evidence cite:
    finding_evidence rows (investigations) plus the evidence refs frozen
    in the verify section content (analyses — a private analysis wrote NO
    evidence rows, the section JSON is the only record)."""
    ids: set[int] = set()
    cur = await conn.execute(
        "SELECT DISTINCT fe.document_id FROM finding_evidence fe"
        " JOIN finding f ON f.id = fe.finding_id"
        " WHERE f.dossier_id = %s", (dossier_id,))
    ids.update(r["document_id"] for r in await cur.fetchall())
    cur = await conn.execute(
        "SELECT content FROM dossier_section"
        " WHERE dossier_id = %s AND stage = 'verify'", (dossier_id,))
    row = await cur.fetchone()
    if row is not None and isinstance(row["content"], dict):
        for claim in row["content"].get("claims") or []:
            if not isinstance(claim, dict):
                continue
            for ref in claim.get("evidence") or []:
                if isinstance(ref, dict) and isinstance(
                        ref.get("document_id"), int):
                    ids.add(ref["document_id"])
    return ids


async def _private_cited(conn: psycopg.AsyncConnection, dossier_id: int,
                         ) -> list[Mapping[str, Any]]:
    doc_ids = sorted(await cited_documents(conn, dossier_id))
    if not doc_ids:
        return []
    cur = await conn.execute(
        "SELECT id, title, owner_id FROM document"
        " WHERE id = ANY(%s) AND visibility = 'private' ORDER BY id",
        (doc_ids,))
    return await cur.fetchall()


async def materialize_deferred_edges(conn: psycopg.AsyncConnection,
                                     dossier_id: int) -> list[int]:
    """Insert the grade-2 causal edges a private investigation deferred
    (finding.payload['link'], design §1 gate 3); idempotent — findings
    that already carry an edge_id are skipped. Returns the edge ids."""
    cur = await conn.execute(
        "SELECT id, payload, speculation, confidence FROM finding"
        " WHERE dossier_id = %s AND edge_id IS NULL"
        " AND payload ? 'link' ORDER BY id", (dossier_id,))
    findings = await cur.fetchall()
    edge_ids: list[int] = []
    for finding in findings:
        payload = finding["payload"] if isinstance(finding["payload"],
                                                   dict) else {}
        link = payload.get("link")
        if not isinstance(link, dict):
            continue
        ecur = await conn.execute(
            "SELECT document_id, quote FROM finding_evidence"
            " WHERE finding_id = %s ORDER BY id", (finding["id"],))
        evidence = [{"document_id": e["document_id"], "quote": e["quote"]}
                    for e in await ecur.fetchall()]
        edge_id = await edge_dao.insert_causal(
            conn,
            src_type=link["src_type"], src_id=link["src_id"],
            dst_type=link["dst_type"], dst_id=link["dst_id"],
            relation=link["relation"],
            properties=_edge_properties(payload, evidence,
                                        bool(finding["speculation"]),
                                        int(finding["id"])),
            provenance_document_id=(evidence[0]["document_id"]
                                    if evidence else None),
            provenance_dossier_id=dossier_id,
            confidence=finding["confidence"], grade=2)
        async with conn.transaction():
            await conn.execute(
                "UPDATE finding SET edge_id = %s WHERE id = %s",
                (edge_id, finding["id"]))
        edge_ids.append(edge_id)
    return edge_ids


async def change_visibility(conn: psycopg.AsyncConnection, *,
                            dossier_id: int, owner_id: int,
                            current: str, target: str,
                            confirm_documents: bool,
                            ) -> DossierVisibilityResult:
    """The share cascade. The router has already resolved the dossier
    (visibility predicate -> 404) and authorized the caller (owner or
    admin -> 403); ``owner_id`` is the DOSSIER owner.

    Raises VisibilityChangeRejected (-> 409) for: shared->private,
    foreign private cited docs (impossible by gate 3, defensive), and the
    unconfirmed auto-share (carries the confirmation list).
    """
    if target == current:
        return DossierVisibilityResult(id=dossier_id, visibility=current)
    if target == "private":
        raise VisibilityChangeRejected(
            "cannot_unshare",
            "a shared dossier cannot be made private — its results have"
            " already compounded into the shared knowledge base")

    private_docs = await _private_cited(conn, dossier_id)
    foreign = [d for d in private_docs if d["owner_id"] != owner_id]
    if foreign:
        raise VisibilityChangeRejected(
            "foreign_private_documents",
            "cited private documents belong to another user: "
            + ", ".join(str(d["id"]) for d in foreign))
    if private_docs and not confirm_documents:
        raise VisibilityChangeRejected(
            "confirm_documents",
            "sharing this dossier will also share the private documents"
            " it cites — retry with confirm_documents=true",
            documents=[ShareDocumentRef(id=d["id"], title=d["title"])
                       for d in private_docs])

    shared_ids: list[int] = []
    for doc in private_docs:
        await doc_dao.set_visibility(conn, doc["id"], "shared")
        shared_ids.append(doc["id"])
    await materialize_deferred_edges(conn, dossier_id)
    async with conn.transaction():
        await conn.execute(
            "UPDATE dossier SET visibility = 'shared' WHERE id = %s",
            (dossier_id,))
    return DossierVisibilityResult(id=dossier_id, visibility="shared",
                                   shared_document_ids=shared_ids)
