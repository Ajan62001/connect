from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from connect.api.deps import get_container
from connect.domain.models import (
    DocumentPage,
    EnrichmentEntityRef,
    EntityDetail,
    EntityPage,
    EntityViews,
    TopicViews,
)
from connect.knowledge.linking import position_tracker
from connect.orchestration.container import Container
from connect.storage import entities as entity_dao
from connect.storage import statements as statement_dao

router = APIRouter(prefix="/entities", tags=["entities"])

MAX_PAGE_SIZE = 100


def _entity_ref(container: Container,
                entity_id: int) -> EnrichmentEntityRef:
    row = container.db.execute(
        "SELECT id, name, entity_type FROM entity WHERE id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return EnrichmentEntityRef(id=row["id"], name=row["name"],
                               entity_type=row["entity_type"])


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


@router.get("/{entity_id}/views", response_model=EntityViews)
async def entity_views(entity_id: int,
                       container: Container = Depends(get_container)):
    """The views index: topics the entity has spoken on, with statement /
    shift aggregates (statement_count desc)."""
    ref = _entity_ref(container, entity_id)
    return EntityViews(
        entity=ref,
        topics=statement_dao.views_for_entity(container.db, entity_id))


@router.get("/{entity_id}/views/{topic}", response_model=TopicViews)
async def entity_topic_views(entity_id: int, topic: str,
                             container: Container = Depends(get_container)):
    """The per-topic view: cached evolution summary (lazily regenerated
    here when stale — governed; a governor block serves the stale text with
    stale=true), the statement timeline (newest first) and open shifts."""
    _entity_ref(container, entity_id)
    governor = container.governor
    assert governor is not None
    summary = await position_tracker.get_view_summary(
        container.db, container.llm, governor, entity_id, topic)
    return TopicViews(
        topic=topic,
        evolution_summary=summary,
        statements=statement_dao.statements_for_topic(
            container.db, entity_id, topic),
        shifts=statement_dao.shifts_for_topic(
            container.db, entity_id, topic))


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
