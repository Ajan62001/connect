from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from connect.api.deps import get_container
from connect.domain.enums import ENRICHMENT_STATUSES
from connect.domain.models import DocumentPage
from connect.orchestration.container import Container
from connect.storage import documents as doc_dao

router = APIRouter(tags=["feed"])


@router.get("/feed", response_model=DocumentPage)
async def feed(status: str | None = Query(default=None),
               page: int = Query(default=1, ge=1),
               page_size: int = Query(default=20, ge=1, le=100),
               container: Container = Depends(get_container)):
    """Pipeline-transparency view: newest first, filterable by
    enrichment_status (status chips in the UI)."""
    if status is not None and status not in ENRICHMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(ENRICHMENT_STATUSES)}")
    items, total = doc_dao.list_page(
        container.db, page=page, page_size=page_size,
        enrichment_status=status)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)
