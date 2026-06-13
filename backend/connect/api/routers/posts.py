"""Findings board — user-authored posts, often sourced from a news document.

Owned + shared/private (tenancy design §1, like dossiers): the list is
viewer-scoped (shared posts + the caller's own private ones); a private post
another user cannot see answers 404. Edits/deletes are owner-only. An
optional ``document_id`` links the finding to the article it came from; the
author must be able to see that document.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

import psycopg

from connect.api.deps import get_container, get_current_user, get_db
from connect.api.qa import grounded_answer
from connect.domain.models import (
    CurrentUser,
    DocumentAnswer,
    DocumentAskRequest,
    Post,
    PostCreate,
    PostUpdate,
)
from connect.orchestration.container import Container
from connect.storage import documents as doc_dao
from connect.storage import posts as post_dao

router = APIRouter(prefix="/posts", tags=["posts"])

MAX_LIMIT = 100

_POST_QA_SYSTEM = (
    "You answer the user's question about a saved FINDING and its cited source,"
    " using ONLY the supplied finding text and document — never outside"
    " knowledge. If they do not answer it, set grounded=false and say so."
    " When grounded, quote a short verbatim excerpt that supports the answer."
    " Be concise.")


@router.get("", response_model=list[Post])
async def list_posts(document_id: int | None = Query(default=None),
                     workspace_id: int | None = Query(default=None),
                     limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
                     offset: int = Query(default=0, ge=0),
                     db: psycopg.AsyncConnection = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    """Newest-first findings the caller may see; ``document_id`` narrows to
    the findings posted from one article, ``workspace_id`` to one workspace."""
    return await post_dao.list_visible(
        db, viewer=user.id, document_id=document_id,
        workspace_id=workspace_id, limit=limit, offset=offset)


@router.post("", response_model=Post, status_code=201)
async def create_post(body: PostCreate,
                      db: psycopg.AsyncConnection = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    if not body.title.strip() or not body.body.strip():
        raise HTTPException(status_code=422,
                            detail="title and body are required")
    if body.document_id is not None:
        # the linked article must exist and be visible to the author
        doc = await doc_dao.get(db, body.document_id, viewer=user.id)
        if doc is None:
            raise HTTPException(status_code=422,
                                detail="linked document not found")
    return await post_dao.insert(
        db, owner_id=user.id, title=body.title.strip(),
        body=body.body.strip(), document_id=body.document_id,
        workspace_id=body.workspace_id,
        visibility=body.visibility or "shared")


@router.get("/{post_id}", response_model=Post)
async def get_post(post_id: int,
                   db: psycopg.AsyncConnection = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    post = await post_dao.get(db, post_id, viewer=user.id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return post


@router.patch("/{post_id}", response_model=Post)
async def patch_post(post_id: int, body: PostUpdate,
                     db: psycopg.AsyncConnection = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    existing = await post_dao.get(db, post_id, viewer=user.id)
    if existing is None:
        raise HTTPException(status_code=404, detail="post not found")
    if existing.owner_id != user.id:
        raise HTTPException(status_code=403,
                            detail="only the author may edit this post")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return existing
    return await post_dao.update(db, post_id, fields, owner_id=user.id)


@router.post("/{post_id}/ask", response_model=DocumentAnswer)
async def ask_post(post_id: int, body: DocumentAskRequest,
                   container: Container = Depends(get_container),
                   db: psycopg.AsyncConnection = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    """Cross-question a finding: a grounded answer from the finding text and
    its cited source document."""
    post = await post_dao.get(db, post_id, viewer=user.id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    if container.llm is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is empty")

    parts = [f"FINDING: {post.title}", post.body]
    if post.document_id is not None:
        doc = await doc_dao.get(db, post.document_id, viewer=user.id)
        if doc is not None:
            parts.append(f"\nCITED DOCUMENT ({doc.title or 'untitled'}):\n"
                         f"{doc.content_text or ''}")
    elif post.quote:
        parts.append(f"\nCITED QUOTE: \"{post.quote}\"")
    return await grounded_answer(
        container.llm, container.governor, db, system=_POST_QA_SYSTEM,
        context="\n".join(parts), question=question, user_id=user.id,
        purpose="post_qa")


@router.delete("/{post_id}", status_code=204, response_class=Response)
async def delete_post(post_id: int,
                      db: psycopg.AsyncConnection = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    if not await post_dao.delete(db, post_id, owner_id=user.id):
        raise HTTPException(status_code=404, detail="post not found")
    return Response(status_code=204)
