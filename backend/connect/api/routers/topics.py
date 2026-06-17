"""The controlled T1 topic vocabulary — the choices a user picks from for a
workspace's focus (and any topic filter), so focus targets real, taggable
topics rather than free text that would never match a document."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from connect.api.deps import get_current_user
from connect.domain.enums import T1_TOPICS
from connect.domain.models import CurrentUser

router = APIRouter(tags=["topics"])


@router.get("/topics", response_model=list[str])
async def list_topics(user: CurrentUser = Depends(get_current_user)):
    """The full controlled topic vocabulary, in vocabulary order."""
    return list(T1_TOPICS)
