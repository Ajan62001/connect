from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from connect.api.deps import get_container
from connect.domain.models import Document, DocumentPage, IngestResult
from connect.ingestion.extract_html import ExtractionError
from connect.ingestion.fetcher import FetchDisallowed, FetchError
from connect.orchestration.container import Container
from connect.storage import documents as doc_dao
from connect.storage import enrichment as enrichment_dao
from connect.storage import fts as fts_dao
from connect.storage import links as link_dao

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_PAGE_SIZE = 100


def _ingest_response(result: IngestResult) -> JSONResponse:
    """201 on a new row; 200 + the existing document on an exact dedup hit."""
    return JSONResponse(
        status_code=201 if result.created else 200,
        content=result.document.model_dump())


@router.post("", response_model=Document, status_code=201)
async def ingest_document(request: Request,
                          container: Container = Depends(get_container)):
    """Three request forms: multipart file upload (field 'file');
    JSON {url}; JSON {text, title?}."""
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
                upload.filename or "upload", data, upload.content_type)
            return _ingest_response(result)

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="JSON object expected")

        if body.get("url"):
            result = await pipeline.ingest_url(str(body["url"]))
            return _ingest_response(result)
        if body.get("text"):
            result = pipeline.ingest_text(
                str(body["text"]), title=body.get("title"))
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
                         container: Container = Depends(get_container)):
    if q:
        items, total = fts_dao.search_documents(
            container.db, q, page=page, page_size=page_size,
            source_id=source_id)
    else:
        items, total = doc_dao.list_page(
            container.db, page=page, page_size=page_size, source_id=source_id)
    return DocumentPage(items=items, total=total, page=page,
                        page_size=page_size)


@router.get("/{doc_id}", response_model=Document)
async def get_document(doc_id: int,
                       container: Container = Depends(get_container)):
    document = doc_dao.get(container.db, doc_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document.model_copy(update={
        "links": link_dao.list_for_document(container.db, doc_id),
        "linked_from": link_dao.linked_from(container.db, doc_id),
        "enrichment": enrichment_dao.get_for_document(container.db, doc_id),
    })
