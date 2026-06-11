from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from connect.api.deps import get_container
from connect.domain.models import Watch, WatchCreate, WatchUpdate
from connect.knowledge import watches as watch_logic
from connect.orchestration.container import Container
from connect.storage import watches as watch_dao

router = APIRouter(prefix="/watches", tags=["watches"])


def _validate(kind: str, query_fts: str | None, entity_id: int | None) -> None:
    if kind in ("topic", "search") and not query_fts:
        raise HTTPException(status_code=422,
                            detail=f"{kind} watches require query_fts")
    if kind == "entity" and entity_id is None:
        raise HTTPException(status_code=422,
                            detail="entity watches require entity_id")


@router.get("", response_model=list[Watch])
async def list_watches(container: Container = Depends(get_container)):
    return watch_dao.list_all(container.db)


@router.post("", response_model=Watch, status_code=201)
async def create_watch(body: WatchCreate,
                       container: Container = Depends(get_container)):
    _validate(body.kind, body.query_fts, body.entity_id)
    return watch_dao.insert(
        container.db, kind=body.kind, label=body.label,
        query_fts=body.query_fts, entity_id=body.entity_id,
        promote=body.promote, muted=body.muted)


@router.get("/badges")
async def badges(container: Container = Depends(get_container)) -> dict[int, int]:
    """{watch_id: unread_count} for all unmuted watches."""
    return watch_logic.badges(container.db)


@router.patch("/{watch_id}", response_model=Watch)
async def patch_watch(watch_id: int, body: WatchUpdate,
                      container: Container = Depends(get_container)):
    existing = watch_dao.get(container.db, watch_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="watch not found")
    fields = body.model_dump(exclude_unset=True)
    merged_query = fields.get("query_fts", existing.query_fts)
    merged_entity = fields.get("entity_id", existing.entity_id)
    _validate(existing.kind, merged_query, merged_entity)
    return watch_dao.update(container.db, watch_id, fields)


@router.delete("/{watch_id}", status_code=204, response_class=Response)
async def delete_watch(watch_id: int,
                       container: Container = Depends(get_container)):
    if not watch_dao.delete(container.db, watch_id):
        raise HTTPException(status_code=404, detail="watch not found")
    return Response(status_code=204)


@router.post("/{watch_id}/seen", response_model=Watch)
async def mark_seen(watch_id: int,
                    container: Container = Depends(get_container)):
    watch = watch_logic.mark_seen(container.db, watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="watch not found")
    return watch
