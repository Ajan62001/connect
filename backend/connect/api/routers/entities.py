from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from connect.api.deps import get_container
from connect.domain.models import DocumentPage, EntityDetail, EntityPage
from connect.orchestration.container import Container
from connect.storage import entities as entity_dao

router = APIRouter(prefix="/entities", tags=["entities"])

MAX_PAGE_SIZE = 100


@router.get("", response_model=EntityPage)
async def list_entities(q: str | None = Query(default=None),
                        page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=20, ge=1,
                                               le=MAX_PAGE_SIZE),
                        container: Container = Depends(get_container)):
    items, total = entity_dao.list_page(
        container.db, q=q, page=page, page_size=page_size)
    return EntityPage(items=items, total=total, page=page,
                      page_size=page_size)


@router.get("/{entity_id}", response_model=EntityDetail)
async def get_entity(entity_id: int,
                     container: Container = Depends(get_container)):
    detail = entity_dao.get_detail(container.db, entity_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return detail


@router.get("/{entity_id}/documents", response_model=DocumentPage)
async def entity_documents(entity_id: int,
                           page: int = Query(default=1, ge=1),
                           page_size: int = Query(default=20, ge=1,
                                                  le=MAX_PAGE_SIZE),
                           container: Container = Depends(get_container)):
    if not entity_dao.exists(container.db, entity_id):
        raise HTTPException(status_code=404, detail="entity not found")
    items, total = entity_dao.documents_for(
        container.db, entity_id, page=page, page_size=page_size)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)
