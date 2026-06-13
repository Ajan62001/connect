from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import psycopg

from connect.api.deps import get_container, get_current_user, get_db
from connect.domain.models import (
    CurrentUser,
    Document,
    DocumentAnswer,
    DocumentAskRequest,
    DocumentPage,
    DocumentVisibilityUpdate,
    IngestResult,
    JobAccepted,
)
from connect.ingestion.extract_html import ExtractionError
from connect.ingestion.fetcher import FetchDisallowed, FetchError
from connect.llm import spend
from connect.llm.provider import LLMError
from connect.llm.spend import BudgetExceeded
from connect.llm.tiers import ModelTier
from connect.orchestration.container import Container
from connect.storage import documents as doc_dao
from connect.storage import enrichment as enrichment_dao
from connect.storage import events as event_dao
from connect.storage import fts as fts_dao
from connect.storage import links as link_dao
from connect.storage import statements as statement_dao

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_PAGE_SIZE = 100

# document Q&A (grounded single-call): the model sees at most this many chars
# of the document; most docs fit whole, long ones are truncated with a note.
PURPOSE_DOC_QA = "document_qa"
DOC_QA_CONTENT_CAP = 30_000
DOC_QA_MAX_TOKENS = 700
DOC_QA_EST_OUT_TOKENS = 500
_DOC_QA_SYSTEM = (
    "You answer the user's question about ONE document, using ONLY that"
    " document's text — never outside knowledge. If the document does not"
    " contain the answer, set grounded=false and say it isn't covered."
    " When grounded, quote a short verbatim excerpt that supports the answer."
    " Be concise and specific.")


def _ingest_response(result: IngestResult) -> JSONResponse:
    """201 on a new row; 200 + the existing document on an exact dedup hit."""
    return JSONResponse(
        status_code=201 if result.created else 200,
        content=result.document.model_dump())


@router.post("", response_model=Document, status_code=201)
async def ingest_document(request: Request,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    """Three request forms: multipart file upload (field 'file');
    JSON {url}; JSON {text, title?}.

    Tenancy defaults (design §1): uploads and pasted text are the caller's
    PRIVATE documents (origin user_upload/user_text, owner=:me, share
    action available); URL ingests are public web content — shared,
    system-owned, origin user_url."""
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
            data = await upload.read()
            result = await pipeline.ingest_file(
                db, upload.filename or "upload", data, upload.content_type,
                owner_id=user.id, visibility="private")
            return _ingest_response(result)

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="JSON object expected")

        if body.get("url"):
            result = await pipeline.ingest_url(db, str(body["url"]),
                                               origin="user_url")
            return _ingest_response(result)
        if body.get("text"):
            result = await pipeline.ingest_text(
                db, str(body["text"]), title=body.get("title"),
                owner_id=user.id, visibility="private")
            return _ingest_response(result)
        raise HTTPException(
            status_code=422,
            detail="provide multipart 'file', JSON {url} or JSON {text, title?}")
    except FetchDisallowed as e:
        raise HTTPException(status_code=403, detail=str(e))
    except FetchError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("", response_model=DocumentPage)
async def list_documents(q: str | None = Query(default=None),
                         source_id: int | None = Query(default=None),
                         page: int = Query(default=1, ge=1),
                         page_size: int = Query(default=20, ge=1,
                                                le=MAX_PAGE_SIZE),
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    if q:
        items, total = await fts_dao.search_documents(
            db, q, page=page, page_size=page_size,
            source_id=source_id, viewer=user.id)
    else:
        items, total = await doc_dao.list_page(
            db, page=page, page_size=page_size, source_id=source_id,
            viewer=user.id)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)


@router.get("/{doc_id}", response_model=Document)
async def get_document(doc_id: int,
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    document = await doc_dao.get(db, doc_id, viewer=user.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document.model_copy(update={
        "links": await link_dao.list_for_document(db, doc_id),
        "linked_from": await link_dao.linked_from(db, doc_id,
                                                  viewer=user.id),
        "enrichment": await enrichment_dao.get_for_document(db, doc_id),
        "event": await event_dao.event_for_document(db, doc_id),
        "statements": await statement_dao.statements_for_document(
            db, doc_id),
    })


@router.post("/{doc_id}/ask", response_model=DocumentAnswer)
async def ask_document(doc_id: int, body: DocumentAskRequest,
                       container: Container = Depends(get_container),
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    """Ask a free-text question about ONE document; returns a grounded answer
    (with a verbatim supporting quote) derived only from that document's
    stored text. A synchronous, governed single LLM call — NOT the
    investigation engine. Respects document visibility (a private doc you
    cannot see is 404)."""
    document = await doc_dao.get(db, doc_id, viewer=user.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if container.llm is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is empty")

    content = document.content_text or ""
    truncated = len(content) > DOC_QA_CONTENT_CAP
    if truncated:
        content = content[:DOC_QA_CONTENT_CAP] + "\n…[document truncated]"
    user_prompt = (f"QUESTION:\n{question}\n\n"
                   f"DOCUMENT (title: {document.title or 'untitled'}):\n"
                   f"{content}")

    # Phase D tenant budget gate (general envelope; purpose excluded from the
    # investigation governor) -> friendly 429 BEFORE spending.
    est_in = (len(_DOC_QA_SYSTEM) + len(user_prompt)) // 4 + 200
    projected = spend.cost_usd(
        container.llm.model_for(ModelTier.BALANCED),
        input_tokens=est_in, output_tokens=DOC_QA_EST_OUT_TOKENS)
    try:
        await container.governor.check(projected, user_id=user.id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

    try:
        completion = await container.llm.complete_structured(
            system=_DOC_QA_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            schema=DocumentAnswer, tier=ModelTier.BALANCED,
            max_tokens=DOC_QA_MAX_TOKENS)
    except LLMError as e:
        raise HTTPException(status_code=502,
                            detail=f"could not answer: {e}") from e

    await spend.record_call(db, purpose=PURPOSE_DOC_QA,
                            model=completion.model, usage=completion.usage,
                            user_id=user.id)
    return completion.output


@router.patch("/{doc_id}", response_model=Document)
async def patch_document(doc_id: int, body: DocumentVisibilityUpdate,
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    """Owner-only visibility flip. private->shared requires no cascade —
    the next enrichment sweep picks the doc up (design §1). shared->
    private is 409 once the doc has compounded into the shared KB
    (mentions/claims/statements would violate I1 retroactively)."""
    document = await doc_dao.get(db, doc_id, viewer=user.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if document.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the document owner may change its visibility")
    if body.visibility != document.visibility:
        if body.visibility == "private" \
                and await doc_dao.has_shared_derivatives(db, doc_id):
            raise HTTPException(status_code=409, detail=(
                "this document has already been enriched into the shared"
                " knowledge base and cannot be made private"))
        await doc_dao.set_visibility(db, doc_id, body.visibility)
        document = await doc_dao.get(db, doc_id, viewer=user.id)
        assert document is not None
    return document


@router.post("/{doc_id}/promote", response_model=JobAccepted,
             status_code=202)
async def promote_document(doc_id: int,
                           container: Container = Depends(get_container),
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    """Queue a manual T2 promotion (event clustering + story threading;
    runs T1 first when the document hasn't been enriched yet)."""
    jobs = container.jobs
    assert container.enrichment is not None and jobs is not None
    document = await doc_dao.get(db, doc_id, viewer=user.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if document.visibility != "shared":
        # I1 gate 1: promotion writes events/edges into the shared KB
        raise HTTPException(status_code=409, detail=(
            "a private document cannot be promoted into the shared"
            " knowledge base — share it first"))
    # manual promote charges the REQUESTING user (tenancy design §4):
    # the job owner becomes the ambient spend user for the T1+T2 calls
    job_id = await jobs.enqueue("enrich_t2", {"document_id": doc_id},
                                owner_id=user.id)
    return JobAccepted(job_id=job_id)


@router.post("/{doc_id}/enrich", response_model=JobAccepted, status_code=202)
async def enrich_document(doc_id: int,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    """Queue a synchronous T1 enrichment of ONE document (summary + news type
    + topics) — the per-document counterpart of the feed sweep. Bounded (one
    FAST call) and governor-checked; charges the requesting user."""
    jobs = container.jobs
    assert container.enrichment is not None and jobs is not None
    document = await doc_dao.get(db, doc_id, viewer=user.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if document.visibility != "shared":
        # I1 gate 1: T1 persistence writes shared-KB rows (topics/enrichment)
        raise HTTPException(status_code=409, detail=(
            "a private document cannot be enriched into the shared knowledge"
            " base — share it first"))
    job_id = await jobs.enqueue("enrich_t1_sync", {"document_id": doc_id},
                                owner_id=user.id)
    return JobAccepted(job_id=job_id)
