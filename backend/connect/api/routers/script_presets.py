"""Script-type presets — built-ins (code) + custom rows (v27).

GET returns the built-ins first (ids = slugs, ``builtin: true``) then the custom
presets (ids ``custom:<n>``). Any authed user creates a custom preset; editing or
deleting one is owner-or-admin. Built-in slugs are immutable (403 on mutate).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

import psycopg

from connect.api.deps import get_current_user, get_db
from connect.content.presets import (
    BUILTIN_PRESETS,
    ScriptPreset,
    ScriptPresetCreate,
    ScriptPresetUpdate,
)
from connect.domain.models import CurrentUser
from connect.storage import script_presets as preset_dao

router = APIRouter(prefix="/script-presets", tags=["script-presets"])


@router.get("", response_model=list[ScriptPreset])
async def list_presets(db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    builtins = list(BUILTIN_PRESETS.values())
    customs = await preset_dao.list_all(db)
    return builtins + customs


@router.post("", response_model=ScriptPreset, status_code=201)
async def create_preset(body: ScriptPresetCreate,
                        db: psycopg.AsyncConnection = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    return await preset_dao.insert(db, owner_id=user.id,
                                   data=body.model_dump())


def _custom_id(preset_id: str) -> int:
    """Parse a 'custom:<n>' id -> n. 403 for a builtin slug, 404 for garbage."""
    if not preset_id.startswith(preset_dao.CUSTOM_PREFIX):
        raise HTTPException(status_code=403,
                            detail="built-in presets cannot be edited")
    try:
        return int(preset_id[len(preset_dao.CUSTOM_PREFIX):])
    except ValueError:
        raise HTTPException(status_code=404, detail="preset not found")


async def _require_owner_or_admin(db: psycopg.AsyncConnection, pid: int,
                                  user: CurrentUser) -> None:
    owner = await preset_dao.owner_of(db, pid)
    if owner is None:
        raise HTTPException(status_code=404, detail="preset not found")
    if owner != user.id and user.role != "admin":
        raise HTTPException(status_code=403,
                            detail="only the owner or an admin may edit this"
                                   " preset")


@router.patch("/{preset_id}", response_model=ScriptPreset)
async def update_preset(preset_id: str, body: ScriptPresetUpdate,
                        db: psycopg.AsyncConnection = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    pid = _custom_id(preset_id)
    await _require_owner_or_admin(db, pid, user)
    updated = await preset_dao.update(
        db, pid, patch=body.model_dump(exclude_unset=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return updated


@router.delete("/{preset_id}", status_code=204, response_class=Response)
async def delete_preset(preset_id: str,
                        db: psycopg.AsyncConnection = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    pid = _custom_id(preset_id)
    await _require_owner_or_admin(db, pid, user)
    await preset_dao.delete(db, pid)
    return Response(status_code=204)
