"""App factory + lifespan: migrate -> seed -> poller (if enabled) -> job
runner; clean shutdown. All routes live under /api.

Run:  uvicorn connect.api.main:app --port 8000
  or: uvicorn --factory connect.api.main:create_app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from connect.api.routers import (
    analyses,
    brief,
    calendar,
    contradictions,
    cursors,
    document_links,
    documents,
    enrichment,
    entities,
    events,
    feed,
    health,
    search,
    sources,
    spend,
    threads,
    watches,
)
from connect.orchestration.config import Settings
from connect.orchestration.container import Container

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    container = Container(settings or Settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container.startup()           # open DB, migrate, seed, wire pipeline
        container.start_background()  # poller (if enabled)
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(title="connect", version="0.1.0", lifespan=lifespan)
    app.state.container = container

    app.include_router(health.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    app.include_router(document_links.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(feed.router, prefix="/api")
    app.include_router(watches.router, prefix="/api")
    app.include_router(entities.router, prefix="/api")
    app.include_router(enrichment.router, prefix="/api")
    app.include_router(spend.router, prefix="/api")
    app.include_router(brief.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    app.include_router(threads.router, prefix="/api")
    app.include_router(cursors.router, prefix="/api")
    app.include_router(calendar.router, prefix="/api")
    app.include_router(analyses.router, prefix="/api")
    app.include_router(contradictions.router, prefix="/api")
    return app


# Default ASGI entrypoint (`uvicorn connect.api.main:app`). Construction is
# side-effect-free: all I/O (DB open, migrate, seed, poller) happens in the
# lifespan, so importing this module stays cheap and offline.
app = create_app()
