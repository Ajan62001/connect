"""Characters — the shared roster of presenter personas (v27).

Any authed user lists/creates; editing or deleting is owner-or-admin (mirrors
the campaign-delete posture). Deleting is safe — every reference is
ON DELETE SET NULL, so a bound workspace channel just falls back to its default.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

import psycopg

from connect.api.deps import get_current_user, get_db
from connect.domain.models import (
    Character,
    CharacterCreate,
    CharacterUpdate,
    CurrentUser,
)
from connect.storage import characters as character_dao

router = APIRouter(prefix="/characters", tags=["characters"])


@router.get("", response_model=list[Character])
async def list_characters(db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    return await character_dao.list_all(db)


@router.post("", response_model=Character, status_code=201)
async def create_character(body: CharacterCreate,
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    return await character_dao.insert(db, owner_id=user.id,
                                      data=body.model_dump())


@router.get("/{character_id}", response_model=Character)
async def get_character(character_id: int,
                        db: psycopg.AsyncConnection = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    character = await character_dao.get(db, character_id)
    if character is None:
        raise HTTPException(status_code=404, detail="character not found")
    return character


async def _require_owner_or_admin(db: psycopg.AsyncConnection,
                                  character_id: int,
                                  user: CurrentUser) -> None:
    owner = await character_dao.owner_of(db, character_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="character not found")
    if owner != user.id and user.role != "admin":
        raise HTTPException(status_code=403,
                            detail="only the owner or an admin may edit this"
                                   " character")


@router.patch("/{character_id}", response_model=Character)
async def update_character(character_id: int, body: CharacterUpdate,
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    await _require_owner_or_admin(db, character_id, user)
    updated = await character_dao.update(
        db, character_id, patch=body.model_dump(exclude_unset=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="character not found")
    return updated


@router.delete("/{character_id}", status_code=204, response_class=Response)
async def delete_character(character_id: int,
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    await _require_owner_or_admin(db, character_id, user)
    await character_dao.delete(db, character_id)
    return Response(status_code=204)
