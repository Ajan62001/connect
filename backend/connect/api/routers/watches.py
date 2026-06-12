from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

import psycopg

from connect.api.deps import get_current_user, get_db
from connect.domain.models import CurrentUser, Watch, WatchCreate, WatchUpdate
from connect.knowledge import watches as watch_logic
from connect.storage import watches as watch_dao

router = APIRouter(prefix="/watches", tags=["watches"])

# Watches are PERSONAL (tenancy design §5): every query here is scoped
# WHERE user_id = :me — another user's watch answers 404, never 403 (its
# existence is not disclosed).


def _validate(kind: str, query_fts: str | None, entity_id: int | None) -> None:
    if kind in ("topic", "search") and not query_fts:
        raise HTTPException(status_code=422,
                            detail=f"{kind} watches require query_fts")
    if kind == "entity" and entity_id is None:
        raise HTTPException(status_code=422,
                            detail="entity watches require entity_id")


@router.get("", response_model=list[Watch])
async def list_watches(workspace_id: int | None = Query(default=None),
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    return await watch_dao.list_all(db, user.id, workspace_id=workspace_id)


@router.post("", response_model=Watch, status_code=201)
async def create_watch(body: WatchCreate,
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    _validate(body.kind, body.query_fts, body.entity_id)
    return await watch_dao.insert(
        db, user_id=user.id, kind=body.kind, label=body.label,
        query_fts=body.query_fts, entity_id=body.entity_id,
        promote=body.promote, muted=body.muted,
        workspace_id=body.workspace_id)


@router.get("/badges")
async def badges(db: psycopg.AsyncConnection = Depends(get_db),
                 user: CurrentUser = Depends(get_current_user),
                 ) -> dict[int, int]:
    """{watch_id: unread_count} for the caller's unmuted watches."""
    return await watch_logic.badges(db, user.id)


@router.patch("/{watch_id}", response_model=Watch)
async def patch_watch(watch_id: int, body: WatchUpdate,
                      db: psycopg.AsyncConnection = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    existing = await watch_dao.get(db, watch_id, user_id=user.id)
    if existing is None:
        raise HTTPException(status_code=404, detail="watch not found")
    fields = body.model_dump(exclude_unset=True)
    merged_query = fields.get("query_fts", existing.query_fts)
    merged_entity = fields.get("entity_id", existing.entity_id)
    _validate(existing.kind, merged_query, merged_entity)
    return await watch_dao.update(db, watch_id, fields, user_id=user.id)


@router.delete("/{watch_id}", status_code=204, response_class=Response)
async def delete_watch(watch_id: int,
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    if not await watch_dao.delete(db, watch_id, user_id=user.id):
        raise HTTPException(status_code=404, detail="watch not found")
    return Response(status_code=204)


@router.post("/{watch_id}/seen", response_model=Watch)
async def mark_seen(watch_id: int,
                    db: psycopg.AsyncConnection = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    watch = await watch_logic.mark_seen(db, watch_id, user.id)
    if watch is None:
        raise HTTPException(status_code=404, detail="watch not found")
    return watch
