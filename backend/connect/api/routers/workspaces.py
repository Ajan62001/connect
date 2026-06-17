"""Workspaces — a saved lens over the shared corpus.

CRUD mirrors the findings board (owned + shared/private; a private workspace
another user cannot see is 404; edits/deletes are owner-only). The focused
feed (`GET /{id}/feed`) applies the workspace's focus to the shared,
viewer-scoped document corpus.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

import psycopg

from sse_starlette.sse import EventSourceResponse

from connect.agents.workspace_agent import run_workspace_agent
from connect.api.deps import get_container, get_current_user, get_db
from connect.api.sse import job_event_stream
from connect.domain.models import (
    ChatTurn,
    CurrentUser,
    Document,
    DocumentPage,
    JobAccepted,
    PostSettings,
    SocialDraft,
    Workspace,
    WorkspaceChatAsyncAccepted,
    WorkspaceChatDetail,
    WorkspaceChatRequest,
    WorkspaceChatResponse,
    WorkspaceChatSummary,
    WorkspaceCreate,
    WorkspaceSourceSuggestions,
    WorkspaceUpdate,
)
from connect.ingestion.extract_html import ExtractionError
from connect.ingestion.fetcher import FetchDisallowed, FetchError
from connect.llm.provider import LLMError
from connect.llm.spend import BudgetExceeded
from connect.orchestration.container import Container
from connect.social import settings as post_settings
from connect.storage import documents as doc_dao
from connect.storage import social_drafts as social_draft_dao
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


@router.get("/{workspace_id}/source-suggestions",
            response_model=WorkspaceSourceSuggestions)
async def workspace_source_suggestions(
        workspace_id: int,
        db: psycopg.AsyncConnection = Depends(get_db),
        user: CurrentUser = Depends(get_current_user)):
    """Sources for managing the workspace focus: those already in focus plus
    topic-matched candidates (sources that publish on the workspace's
    topics)."""
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    data = await workspace_dao.source_suggestions(db, ws, viewer=user.id)
    return WorkspaceSourceSuggestions(**data)


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


@router.get("/{workspace_id}/post-settings", response_model=PostSettings)
async def workspace_post_settings(workspace_id: int,
                                  db: psycopg.AsyncConnection = Depends(get_db),
                                  user: CurrentUser = Depends(get_current_user)):
    """The effective post-generation settings for this workspace (the global
    default with the workspace's overrides applied)."""
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return await post_settings.effective(db, ws)


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


@router.get("/{workspace_id}/documents", response_model=DocumentPage)
async def list_workspace_documents(workspace_id: int,
                                   page: int = Query(default=1, ge=1),
                                   page_size: int = Query(default=20, ge=1,
                                                          le=MAX_PAGE_SIZE),
                                   db: psycopg.AsyncConnection = Depends(get_db),
                                   user: CurrentUser = Depends(get_current_user)):
    """The workspace's OWN knowledge-base documents (added to it directly)."""
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    items, total = await doc_dao.list_in_workspace(
        db, workspace_id, viewer=user.id, page=page, page_size=page_size)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)


@router.post("/{workspace_id}/documents", response_model=Document)
async def add_workspace_document(workspace_id: int, request: Request,
                                 container: Container = Depends(get_container),
                                 db: psycopg.AsyncConnection = Depends(get_db),
                                 user: CurrentUser = Depends(get_current_user)):
    """Add a document to this workspace's knowledge base. Same three forms as
    POST /documents (multipart 'file'; JSON {url}; JSON {text, title?}); the
    ingested document is tagged into the workspace."""
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    pipeline = container.pipeline
    assert pipeline is not None
    content_type = request.headers.get("content-type", "")

    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise HTTPException(status_code=422,
                                    detail="multipart field 'file' is required")
            result = await pipeline.ingest_file(
                db, upload.filename or "upload", await upload.read(),
                upload.content_type, owner_id=user.id, visibility="private")
        else:
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HTTPException(status_code=422, detail="invalid JSON body")
            if not isinstance(body, dict):
                raise HTTPException(status_code=422, detail="JSON object expected")
            if body.get("url"):
                result = await pipeline.ingest_url(db, str(body["url"]),
                                                   origin="user_url")
            elif body.get("text"):
                result = await pipeline.ingest_text(
                    db, str(body["text"]), title=body.get("title"),
                    owner_id=user.id, visibility="private")
            else:
                raise HTTPException(
                    status_code=422,
                    detail="provide multipart 'file', JSON {url} or {text}")
    except FetchDisallowed as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except FetchError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    await doc_dao.set_workspace(db, result.document.id, workspace_id)
    return JSONResponse(
        status_code=201 if result.created else 200,
        content=result.document.model_dump())


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
    transcript.append(chat_dao.turn_dict("user", message))

    try:
        result = await run_workspace_agent(
            db, llm=container.llm, governor=container.governor,
            embedder=container.embedder, vectors=container.vectors,
            workspace=ws, viewer=user.id, messages=wire,
            card_store=container.card_store,
            logo_store=container.logo_store,
            post_settings=await post_settings.effective(db, ws),
            mode="quick")
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except LLMError as e:
        raise HTTPException(status_code=502,
                            detail=f"the assistant could not respond: {e}") from e

    transcript.append(chat_dao.turn_dict("assistant", result.final_answer,
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


@router.post("/{workspace_id}/chat/async",
             response_model=WorkspaceChatAsyncAccepted, status_code=202)
async def start_async_chat(workspace_id: int, body: WorkspaceChatRequest,
                           container: Container = Depends(get_container),
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    """Run a "deep" agent turn asynchronously (a background job with a higher
    budget + iteration ceiling than the synchronous chat). The user message is
    recorded immediately; progress streams over `/chats/{chat_id}/events` and
    the assistant answer lands in the chat transcript on completion."""
    ws = await workspace_dao.get(db, workspace_id, viewer=user.id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if container.llm is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    if container.jobs is None:
        raise HTTPException(status_code=503,
                            detail="background jobs are unavailable")
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
    transcript.append(chat_dao.turn_dict("user", message))

    if body.chat_id is not None:
        await chat_dao.update(db, body.chat_id, owner_id=user.id,
                              messages=wire, transcript=transcript)
        chat_id = body.chat_id
    else:
        chat_id = await chat_dao.create(
            db, workspace_id=workspace_id, owner_id=user.id,
            title=message[:80], messages=wire, transcript=transcript)

    job_id = await container.jobs.enqueue(
        "workspace_task",
        {"chat_id": chat_id, "workspace_id": workspace_id,
         "owner_id": user.id},
        owner_id=user.id)
    return WorkspaceChatAsyncAccepted(chat_id=chat_id, job_id=job_id)


def _ws_chat_translate(type_: str, data: dict) -> tuple[str, dict] | None:
    """job_event rows -> SSE contract shapes for an async deep run (mirrors the
    investigation translate: generic queue rows mapped, 'iteration' rows pass
    through, terminal rows close the stream)."""
    if type_ == "started":
        return None
    if type_ == "cancelled":
        return "error", {"message": "run cancelled"}
    if type_ == "error":
        return "error", {"message": (data.get("message") or data.get("error")
                                     or "the assistant could not respond")}
    if type_ == "done":
        return "done", {}
    return type_, data


@router.get("/{workspace_id}/chats/{chat_id}/events")
async def async_chat_events(workspace_id: int, chat_id: int, request: Request,
                            after: int = Query(default=0, ge=0),
                            container: Container = Depends(get_container),
                            db: psycopg.AsyncConnection = Depends(get_db),
                            user: CurrentUser = Depends(get_current_user)):
    """SSE progress stream for a deep run (job_event replay by seq, ?after= /
    Last-Event-ID resume) — the same contract as investigations/analyses."""
    chat = await chat_dao.get(db, chat_id, workspace_id=workspace_id,
                              owner_id=user.id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    job_id = await chat_dao.latest_job_id(db, chat_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="chat has no async run")
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    return EventSourceResponse(
        job_event_stream(request, container, job_id, after,
                         _ws_chat_translate))


@router.post("/{workspace_id}/chats/{chat_id}/cancel",
             response_model=JobAccepted, status_code=202)
async def cancel_async_chat(workspace_id: int, chat_id: int,
                            container: Container = Depends(get_container),
                            db: psycopg.AsyncConnection = Depends(get_db),
                            user: CurrentUser = Depends(get_current_user)):
    chat = await chat_dao.get(db, chat_id, workspace_id=workspace_id,
                              owner_id=user.id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    if container.jobs is None:
        raise HTTPException(status_code=503,
                            detail="background jobs are unavailable")
    job_id = await chat_dao.latest_job_id(db, chat_id)
    if job_id is not None:
        await container.jobs.request_cancel(job_id)  # no-op on terminal runs
    return JobAccepted(job_id=job_id or 0)


@router.get("/{workspace_id}/social-drafts", response_model=list[SocialDraft])
async def list_social_drafts(workspace_id: int,
                             db: psycopg.AsyncConnection = Depends(get_db),
                             user: CurrentUser = Depends(get_current_user)):
    """The social-post drafts the agent generated for this workspace (yours,
    newest first). Each card is at /api/social/card/{card_sha}.jpg."""
    return await social_draft_dao.list_for(db, workspace_id, owner_id=user.id)


@router.delete("/{workspace_id}/social-drafts/{draft_id}", status_code=204,
               response_class=Response)
async def delete_social_draft(workspace_id: int, draft_id: int,
                              db: psycopg.AsyncConnection = Depends(get_db),
                              user: CurrentUser = Depends(get_current_user)):
    if not await social_draft_dao.delete(db, draft_id, owner_id=user.id):
        raise HTTPException(status_code=404, detail="draft not found")
    return Response(status_code=204)
