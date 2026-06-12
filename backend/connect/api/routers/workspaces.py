"""Workspaces — a saved lens over the shared corpus.

CRUD mirrors the findings board (owned + shared/private; a private workspace
another user cannot see is 404; edits/deletes are owner-only). The focused
feed (`GET /{id}/feed`) applies the workspace's focus to the shared,
viewer-scoped document corpus.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

import psycopg

from connect.agents.workspace_agent import run_workspace_agent
from connect.api.deps import get_container, get_current_user, get_db
from connect.domain.models import (
    ChatTurn,
    CurrentUser,
    DocumentPage,
    Workspace,
    WorkspaceChatDetail,
    WorkspaceChatRequest,
    WorkspaceChatResponse,
    WorkspaceChatSummary,
    WorkspaceCreate,
    WorkspaceUpdate,
)
from connect.llm.provider import LLMError
from connect.llm.spend import BudgetExceeded
from connect.orchestration.container import Container
from connect.storage import workspace_chats as chat_dao
from connect.storage import workspaces as workspace_dao

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

MAX_PAGE_SIZE = 100


@router.get("", response_model=list[Workspace])
async def list_workspaces(db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    return await workspace_dao.list_visible(db, viewer=user.id)


@router.post("", response_model=Workspace, status_code=201)
async def create_workspace(body: WorkspaceCreate,
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    return await workspace_dao.insert(
        db, owner_id=user.id, name=body.name.strip(),
        description=body.description, topics=body.topics,
        source_ids=body.source_ids, query_fts=body.query_fts,
        visibility=body.visibility or "shared")


@router.get("/{workspace_id}", response_model=Workspace)
async def get_workspace(workspace_id: int,
                        db: psycopg.AsyncConnection = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return ws


@router.patch("/{workspace_id}", response_model=Workspace)
async def patch_workspace(workspace_id: int, body: WorkspaceUpdate,
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    existing = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if existing is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if existing.owner_id != user.id:
        raise HTTPException(status_code=403,
                            detail="only the owner may edit this workspace")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return existing
    return await workspace_dao.update(db, workspace_id, fields,
                                      owner_id=user.id)


@router.delete("/{workspace_id}", status_code=204, response_class=Response)
async def delete_workspace(workspace_id: int,
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    if not await workspace_dao.delete(db, workspace_id, owner_id=user.id):
        raise HTTPException(status_code=404, detail="workspace not found")
    return Response(status_code=204)


@router.get("/{workspace_id}/feed", response_model=DocumentPage)
async def workspace_feed(workspace_id: int,
                         page: int = Query(default=1, ge=1),
                         page_size: int = Query(default=20, ge=1,
                                                le=MAX_PAGE_SIZE),
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    """The workspace's focused feed: shared-corpus documents matching its
    focus, newest first, viewer-scoped."""
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    items, total = await workspace_dao.focused_feed(
        db, ws, viewer=user.id, page=page, page_size=page_size)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)


def _turn_dict(role: str, text: str, *, tools_used: list[str] | None = None,
               finding=None) -> dict:
    return {"role": role, "text": text,
            "tools_used": tools_used or [],
            "finding_id": finding.id if finding else None,
            "finding_title": finding.title if finding else None}


@router.post("/{workspace_id}/chat", response_model=WorkspaceChatResponse)
async def workspace_chat(workspace_id: int, body: WorkspaceChatRequest,
                         container: Container = Depends(get_container),
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    """Send one message to the workspace's AI agent. Omit chat_id to start a
    new conversation; pass it to continue a saved one. The agent searches +
    reads this workspace's documents, grounds its answer, and can post a
    finding. Conversations are saved (per user, per workspace)."""
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if container.llm is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is empty")

    if body.chat_id is not None:
        chat = await chat_dao.get(db, body.chat_id, workspace_id=workspace_id,
                                  owner_id=user.id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        wire = list(chat["messages"] or [])
        transcript = list(chat["transcript"] or [])
    else:
        wire, transcript = [], []

    wire.append({"role": "user", "content": message})
    transcript.append(_turn_dict("user", message))

    try:
        result = await run_workspace_agent(
            db, llm=container.llm, governor=container.governor,
            embedder=container.embedder, vectors=container.vectors,
            workspace=ws, viewer=user.id, messages=wire)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except LLMError as e:
        raise HTTPException(status_code=502,
                            detail=f"the assistant could not respond: {e}") from e

    transcript.append(_turn_dict("assistant", result.final_answer,
                                 tools_used=result.tools_used,
                                 finding=result.finding))
    if body.chat_id is not None:
        await chat_dao.update(db, body.chat_id, owner_id=user.id,
                              messages=result.messages, transcript=transcript)
        chat_id = body.chat_id
    else:
        chat_id = await chat_dao.create(
            db, workspace_id=workspace_id, owner_id=user.id,
            title=message[:80], messages=result.messages,
            transcript=transcript)

    return WorkspaceChatResponse(
        chat_id=chat_id, reply=result.final_answer,
        transcript=[ChatTurn(**t) for t in transcript],
        tools_used=result.tools_used, finding=result.finding,
        turns_completed=result.turns_completed, spent_usd=result.spent_usd,
        budget_remaining_usd=result.budget_remaining_usd)


@router.get("/{workspace_id}/chats", response_model=list[WorkspaceChatSummary])
async def list_chats(workspace_id: int,
                     db: psycopg.AsyncConnection = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    """The caller's saved conversations in this workspace (newest first)."""
    rows = await chat_dao.list_for(db, workspace_id, user.id)
    return [WorkspaceChatSummary(
        id=r["id"], title=r["title"], created_at=r["created_at"],
        updated_at=r["updated_at"]) for r in rows]


@router.get("/{workspace_id}/chats/{chat_id}",
            response_model=WorkspaceChatDetail)
async def get_chat(workspace_id: int, chat_id: int,
                   db: psycopg.AsyncConnection = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    chat = await chat_dao.get(db, chat_id, workspace_id=workspace_id,
                              owner_id=user.id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return WorkspaceChatDetail(
        id=chat["id"], title=chat["title"],
        transcript=[ChatTurn(**t) for t in (chat["transcript"] or [])],
        created_at=chat["created_at"], updated_at=chat["updated_at"])


@router.delete("/{workspace_id}/chats/{chat_id}", status_code=204,
               response_class=Response)
async def delete_chat(workspace_id: int, chat_id: int,
                      db: psycopg.AsyncConnection = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    if not await chat_dao.delete(db, chat_id, owner_id=user.id):
        raise HTTPException(status_code=404, detail="chat not found")
    return Response(status_code=204)
