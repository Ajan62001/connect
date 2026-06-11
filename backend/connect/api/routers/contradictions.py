"""The contradiction ledger surface — filterable list + dismiss."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from connect.api.deps import get_container
from connect.domain.models import ContradictionItem, ContradictionPage
from connect.knowledge import contradictions
from connect.orchestration.container import Container

router = APIRouter(prefix="/contradictions", tags=["contradictions"])

MAX_PAGE_SIZE = 100


@router.get("", response_model=ContradictionPage)
async def list_contradictions(
        status: Literal["open", "dismissed", "resolved"] | None = Query(
            default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
        container: Container = Depends(get_container)):
    items, total = contradictions.list_page(
        container.db, status=status, page=page, page_size=page_size)
    return ContradictionPage(items=items, total=total, page=page,
                             page_size=page_size)


@router.post("/{contradiction_id}/dismiss",
             response_model=ContradictionItem)
async def dismiss_contradiction(contradiction_id: int,
                                container: Container = Depends(
                                    get_container)):
    row = contradictions.dismiss(container.db, contradiction_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="contradiction not found")
    return row
