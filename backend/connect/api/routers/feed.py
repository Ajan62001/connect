from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

import psycopg

from connect.api.deps import get_current_user, get_db
from connect.domain.enums import ENRICHMENT_STATUSES
from connect.domain.models import CurrentUser, DocumentPage, FeedFacets
from connect.storage import documents as doc_dao

router = APIRouter(tags=["feed"])


@router.get("/feed", response_model=DocumentPage)
async def feed(status: str | None = Query(default=None),
               source_id: int | None = Query(default=None),
               news_type: str | None = Query(default=None),
               topic: str | None = Query(default=None),
               page: int = Query(default=1, ge=1),
               page_size: int = Query(default=20, ge=1, le=100),
               db: psycopg.AsyncConnection = Depends(get_db),
               user: CurrentUser = Depends(get_current_user)):
    """Pipeline-transparency view: newest first, filterable by enrichment
    status, source, news type, and topic. Carries the tenancy visibility
    predicate (shared OR mine)."""
    if status is not None and status not in ENRICHMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(ENRICHMENT_STATUSES)}")
    items, total = await doc_dao.list_page(
        db, page=page, page_size=page_size,
        enrichment_status=status, source_id=source_id,
        news_type=news_type, topic=topic, viewer=user.id)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)


@router.get("/feed/facets", response_model=FeedFacets)
async def feed_facets(db: psycopg.AsyncConnection = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    """The distinct filter values present in the viewer-visible corpus —
    populates the feed's source / news-type / topic filters."""
    return FeedFacets(**await doc_dao.feed_facets(db, viewer=user.id))
