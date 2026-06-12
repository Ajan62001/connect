from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

import psycopg

from connect.api.deps import get_container, get_current_user, get_db
from connect.domain.models import CurrentUser, SearchResult
from connect.orchestration.container import Container
from connect.retrieval import search as retrieval

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResult)
async def search(q: str = Query(min_length=1),
                 kind: Literal["all", "documents", "entities"] = Query(
                     default="all"),
                 db: psycopg.AsyncConnection = Depends(get_db),
                 container: Container = Depends(get_container),
                 user: CurrentUser = Depends(get_current_user)):
    # hybrid (design §4): embedder + vector index ride along; with
    # embeddings disabled both degrade and the search is lexical-only.
    # viewer = the requester: shared docs + their own private docs.
    return await retrieval.search(db, q, kind=kind,
                                  embedder=container.embedder,
                                  vectors=container.vectors,
                                  viewer=user.id)
