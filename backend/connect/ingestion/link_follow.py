"""Selective link-follow — bounded, polite, depth 1.

Candidate priority (per parent document, capped at ``max_per_doc``):
  1. official-domain links on a DIFFERENT registrable domain than the
     parent, in document order — cross-domain official references (the
     gazette notification a news story cites);
  2. file links on the SAME registrable domain as the parent (the RBI
     circular page -> its PDF twin + annex PDFs, the flagship case).
Everything else stays status='not_followed' (manually fetchable via the
API). Same-domain non-file links are never auto-followed even when the
domain is official: on rbi.org.in/pib.gov.in EVERY same-site link is
"official", and generic anchors ('Reports', 'Departments') routinely pass
the in-content keep rule — verified against the real RBI corpus, where they
would otherwise exhaust the follow budget before the PDF twin.

Depth 1: followed documents get THEIR links extracted and stored, but are
never auto-followed from (pipeline passes is_link_follow=True down).

A follow NEVER raises out of the parent ingest: failures land on the link
row (status='failed', error). On success — including a dedup hit resolving
to an existing document — the link flips to 'fetched' and a
document-[links_to]->document edge is written (provenance = parent, grade 1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import psycopg
from urllib.parse import urlsplit

from connect.domain.models import Document, DocumentLink
from connect.ingestion.links import registrable_domain
from connect.storage import documents as doc_dao
from connect.storage import edges as edge_dao
from connect.storage import links as link_dao

if TYPE_CHECKING:  # avoid the import cycle (pipeline imports this module)
    from connect.ingestion.pipeline import IngestionPipeline

log = logging.getLogger(__name__)


def select_candidates(links: list[DocumentLink], *,
                      parent_url: str | None,
                      max_per_doc: int) -> list[DocumentLink]:
    """Pure policy: cross-domain officials first, then same-domain file
    links, capped (module docstring has the why)."""
    parent_domain = (registrable_domain(urlsplit(parent_url).hostname or "")
                     if parent_url else None)

    def domain(link: DocumentLink) -> str:
        return registrable_domain(urlsplit(link.url).hostname or "")

    pending = [l for l in links if l.status == "not_followed"]
    official = [l for l in pending
                if l.is_official and domain(l) != parent_domain]
    same_domain_files = [
        l for l in pending
        if l.is_file and parent_domain is not None
        and domain(l) == parent_domain]
    return (official + same_domain_files)[:max_per_doc]


async def follow_link(conn: psycopg.AsyncConnection,
                      pipeline: "IngestionPipeline",
                      link: DocumentLink, *,
                      parent_id: int) -> Document | None:
    """Fetch one link through the normal ingest pipeline; returns the
    resolved document, or None on failure (recorded on the row)."""
    try:
        # A document already at this exact URL needs no re-fetch — resolve
        # straight to it (politeness budget is the scarce resource here).
        document = await doc_dao.get_by_url(conn, link.url)
        if document is None:
            result = await pipeline.ingest_url(conn, link.url,
                                               is_link_follow=True)
            document = result.document  # created=False = content-dedup hit
    except Exception as e:  # noqa: BLE001 — follow failure is data, never fatal
        log.warning("link follow failed for %s: %s", link.url, e)
        await link_dao.mark_failed(conn, link.id, str(e))
        return None
    await link_dao.mark_fetched(conn, link.id, document.id)
    if document.id != parent_id:
        await edge_dao.insert_links_to(conn, parent_id, document.id)
    return document


async def auto_follow(conn: psycopg.AsyncConnection,
                      pipeline: "IngestionPipeline", *,
                      parent_id: int, parent_url: str | None,
                      max_per_doc: int) -> tuple[int, int]:
    """Apply the policy to a parent's stored links; returns (ok, failed).

    Runs strictly AFTER the parent row is committed (link rows and the
    parent document are durable before the first fetch goes out).
    """
    links = await link_dao.list_for_document(conn, parent_id)
    candidates = select_candidates(
        links, parent_url=parent_url, max_per_doc=max_per_doc)
    for link in candidates:  # mark up front: visible work queue
        await link_dao.set_status(conn, link.id, "pending")
    ok = failed = 0
    for link in candidates:
        document = await follow_link(conn, pipeline, link, parent_id=parent_id)
        if document is not None:
            ok += 1
        else:
            failed += 1
    return ok, failed
