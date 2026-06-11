"""Manual on-demand link follow — the same code path the auto-follow uses.

POST /document-links/{link_id}/fetch
  - 404 unknown link id
  - already 'fetched': idempotent, returns the current state (no re-fetch)
  - otherwise runs follow_link; a fetch/extract failure lands on the link
    row (status='failed', error) and returns 200 with document=None — the
    failure is data, not an HTTP error.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import psycopg

from connect.api.deps import get_container, get_db
from connect.domain.models import LinkFetchResult
from connect.ingestion import link_follow
from connect.orchestration.container import Container
from connect.storage import documents as doc_dao
from connect.storage import links as link_dao

router = APIRouter(prefix="/document-links", tags=["documents"])


@router.post("/{link_id}/fetch", response_model=LinkFetchResult)
async def fetch_link(link_id: int,
                     container: Container = Depends(get_container),
                     db: psycopg.AsyncConnection = Depends(get_db)):
    link = await link_dao.get(db, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="link not found")

    if link.status == "fetched" and link.resolved_document_id is not None:
        document = await doc_dao.get(db, link.resolved_document_id)
        return LinkFetchResult(link=link, document=document)

    pipeline = container.pipeline
    assert pipeline is not None
    document = await link_follow.follow_link(
        db, pipeline, link, parent_id=link.document_id)
    refreshed = await link_dao.get(db, link_id)
    assert refreshed is not None
    return LinkFetchResult(link=refreshed, document=document)
