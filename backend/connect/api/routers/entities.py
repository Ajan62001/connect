from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

import psycopg

from connect.api.deps import get_container, get_db
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


async def _entity_ref(db: psycopg.AsyncConnection,
                      entity_id: int) -> EnrichmentEntityRef:
    cur = await db.execute(
        "SELECT id, name, entity_type FROM entity WHERE id = %s",
        (entity_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return EnrichmentEntityRef(id=row["id"], name=row["name"],
                               entity_type=row["entity_type"])


@router.get("", response_model=EntityPage)
async def list_entities(q: str | None = Query(default=None),
                        page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=20, ge=1,
                                               le=MAX_PAGE_SIZE),
                        db: psycopg.AsyncConnection = Depends(get_db)):
    items, total = await entity_dao.list_page(
        db, q=q, page=page, page_size=page_size)
    return EntityPage(items=items, total=total, page=page,
                      page_size=page_size)


@router.get("/{entity_id}", response_model=EntityDetail)
async def get_entity(entity_id: int,
                     db: psycopg.AsyncConnection = Depends(get_db)):
    detail = await entity_dao.get_detail(db, entity_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return detail


@router.get("/{entity_id}/views", response_model=EntityViews)
async def entity_views(entity_id: int,
                       db: psycopg.AsyncConnection = Depends(get_db)):
    """The views index: topics the entity has spoken on, with statement /
    shift aggregates (statement_count desc)."""
    ref = await _entity_ref(db, entity_id)
    return EntityViews(
        entity=ref,
        topics=await statement_dao.views_for_entity(db, entity_id))


@router.get("/{entity_id}/views/{topic}", response_model=TopicViews)
async def entity_topic_views(entity_id: int, topic: str,
                             container: Container = Depends(get_container),
                             db: psycopg.AsyncConnection = Depends(get_db)):
    """The per-topic view: cached evolution summary (lazily regenerated
    here when stale — governed; a governor block serves the stale text with
    stale=true), the statement timeline (newest first) and open shifts."""
    await _entity_ref(db, entity_id)
    governor = container.governor
    assert governor is not None
    summary = await position_tracker.get_view_summary(
        db, container.llm, governor, entity_id, topic)
    return TopicViews(
        topic=topic,
        evolution_summary=summary,
        statements=await statement_dao.statements_for_topic(
            db, entity_id, topic),
        shifts=await statement_dao.shifts_for_topic(
            db, entity_id, topic))


@router.get("/{entity_id}/documents", response_model=DocumentPage)
async def entity_documents(entity_id: int,
                           page: int = Query(default=1, ge=1),
                           page_size: int = Query(default=20, ge=1,
                                                  le=MAX_PAGE_SIZE),
                           db: psycopg.AsyncConnection = Depends(get_db)):
    if not await entity_dao.exists(db, entity_id):
        raise HTTPException(status_code=404, detail="entity not found")
    items, total = await entity_dao.documents_for(
        db, entity_id, page=page, page_size=page_size)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)
