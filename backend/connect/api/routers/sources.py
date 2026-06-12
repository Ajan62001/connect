from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

import psycopg

from connect.api.deps import get_container, get_db, require_admin
from connect.domain.models import (
    BackfillRequest,
    JobAccepted,
    SampleItem,
    Source,
    SourceCreate,
    SourceTestRequest,
    SourceTestResult,
    SourceUpdate,
)
from connect.orchestration.container import Container
from connect.sources.base import AdapterSkip, SourceConfigError, parse_source_config
from connect.sources.registry import POLLABLE_TYPES
from connect.storage import sources as source_dao

router = APIRouter(prefix="/sources", tags=["sources"])


def _validate_config(type_: str, config: dict) -> None:
    try:
        parse_source_config(type_, config)
    except SourceConfigError as e:
        raise HTTPException(status_code=422, detail=f"invalid config: {e}")


@router.get("", response_model=list[Source])
async def list_sources(db: psycopg.AsyncConnection = Depends(get_db)):
    return await source_dao.list_all(db)


# Mutations + manual polls are admin-only (design §5: sources are
# admin-managed; GET stays member-readable for transparency).

@router.post("", response_model=Source, status_code=201,
             dependencies=[Depends(require_admin)])
async def create_source(body: SourceCreate,
                        db: psycopg.AsyncConnection = Depends(get_db)):
    _validate_config(body.type, body.config)
    if await source_dao.get_by_name(db, body.name) is not None:
        raise HTTPException(status_code=409,
                            detail=f"source named {body.name!r} already exists")
    return await source_dao.insert(
        db, name=body.name, type_=body.type, config=body.config,
        credibility_tier=body.credibility_tier, notes=body.notes,
        enabled=body.enabled, t1_exempt=body.t1_exempt)


@router.post("/test", response_model=SourceTestResult,
             dependencies=[Depends(require_admin)])
async def test_source(body: SourceTestRequest,
                      container: Container = Depends(get_container)):
    if body.type == "manual":
        return SourceTestResult(ok=True, sample_items=[])
    adapter = container.adapters.get(body.type)
    if adapter is None:
        return SourceTestResult(ok=False, error=f"no adapter for {body.type!r}")
    try:
        adapter.validate(body.config)
    except SourceConfigError as e:
        return SourceTestResult(ok=False, error=f"invalid config: {e}")
    try:
        items = await adapter.sample(body.config, limit=5)
    except (NotImplementedError, AdapterSkip) as e:
        # AdapterSkip: e.g. 'TWITTERAPI_IO_API_KEY not set' — clean ok:false
        return SourceTestResult(ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001 — test endpoint reports, never 500s
        return SourceTestResult(ok=False, error=str(e))
    return SourceTestResult(ok=True, sample_items=[
        SampleItem(title=i.title, url=i.url, published_at=i.published_at)
        for i in items])


@router.get("/{source_id}", response_model=Source)
async def get_source(source_id: int,
                     db: psycopg.AsyncConnection = Depends(get_db)):
    source = await source_dao.get(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    return source


@router.patch("/{source_id}", response_model=Source,
              dependencies=[Depends(require_admin)])
async def patch_source(source_id: int, body: SourceUpdate,
                       db: psycopg.AsyncConnection = Depends(get_db)):
    existing = await source_dao.get(db, source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="source not found")
    fields = body.model_dump(exclude_unset=True)
    if "config" in fields and fields["config"] is not None:
        _validate_config(existing.type, fields["config"])
    return await source_dao.update(db, source_id, fields)


@router.delete("/{source_id}", status_code=204, response_class=Response,
               dependencies=[Depends(require_admin)])
async def delete_source(source_id: int,
                        db: psycopg.AsyncConnection = Depends(get_db)):
    if not await source_dao.delete(db, source_id):
        raise HTTPException(status_code=404, detail="source not found")
    return Response(status_code=204)


@router.post("/{source_id}/poll", response_model=JobAccepted,
             status_code=202, dependencies=[Depends(require_admin)])
async def poll_source(source_id: int,
                      container: Container = Depends(get_container),
                      db: psycopg.AsyncConnection = Depends(get_db)):
    source = await source_dao.get(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if source.type not in POLLABLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"source type {source.type!r} is not pollable")
    jobs = container.jobs
    assert container.poller is not None and jobs is not None
    job_id = await jobs.enqueue("poll_source", {"source_id": source_id})
    return JobAccepted(job_id=job_id)


@router.post("/{source_id}/backfill", response_model=JobAccepted,
             status_code=202, dependencies=[Depends(require_admin)])
async def backfill_source(source_id: int, body: BackfillRequest,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db)):
    """Enqueue a historical backfill: pull OLDER documents into the corpus via
    sitemap / pagination / Wayback / manual discovery (default: all four).
    The bulk crawl runs at lowest queue priority on a worker."""
    source = await source_dao.get(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    cfg = source.config or {}
    has_domain = bool(cfg.get("index_url") or cfg.get("feed_url"))
    has_manual = bool(body.manual_urls or body.sitemap_urls
                      or body.manual_url_template)
    if not has_domain and not has_manual:
        raise HTTPException(
            status_code=400,
            detail=f"source type {source.type!r} carries no web domain to "
                   "backfill; provide manual_urls / sitemap_urls / "
                   "manual_url_template")
    jobs = container.jobs
    assert jobs is not None
    payload = {"source_id": source_id, **body.model_dump()}
    job_id = await jobs.enqueue("backfill_source", payload)
    return JobAccepted(job_id=job_id)
