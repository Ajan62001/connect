from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from connect.api.deps import get_container
from connect.domain.models import SearchResult
from connect.orchestration.container import Container
from connect.retrieval import search as retrieval

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResult)
async def search(q: str = Query(min_length=1),
                 kind: Literal["all", "documents", "entities"] = Query(
                     default="all"),
                 container: Container = Depends(get_container)):
    return retrieval.search(container.db, q, kind=kind)
