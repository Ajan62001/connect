"""App factory + lifespan: migrate -> seed -> queue + SSE event bus; clean
shutdown. All routes live under /api. Background jobs run in worker
processes (python -m connect.workers.main); the api-flavor container keeps
an embedded execution mode for no-docker dev and tests.

v0.2 Phase B (tenancy design §3): the whole /api surface is behind cookie
auth — every router is included with a get_current_user dependency except
/api/health (compose healthchecks) and /api/auth/* (the login machinery
itself). SessionMiddleware signs ONLY the short-lived oauth_state handshake
cookie; the app session is an opaque server-side row. The Origin-check
middleware is the CSRF second layer.

Run:  uvicorn connect.api.main:app --port 8000
  or: uvicorn --factory connect.api.main:create_app --port 8000
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from starlette.middleware.sessions import SessionMiddleware

from connect.api.deps import get_current_user
from connect.api.limits import RateLimiter, rate_limit
from connect.api.routers import (
    admin,
    analyses,
    auth,
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
    investigations,
    position_shifts,
    posts,
    search,
    social,
    sources,
    spend,
    threads,
    watches,
    workspaces,
)
from connect.api.security import OriginCheckMiddleware
from connect.orchestration.config import Settings
from connect.orchestration.container import Container

log = logging.getLogger(__name__)

# Every business router goes behind auth. admin.py adds require_admin on
# top of this itself.
_AUTHED_ROUTERS = (
    sources.router,
    documents.router,
    document_links.router,
    search.router,
    feed.router,
    watches.router,
    posts.router,
    social.router,
    workspaces.router,
    entities.router,
    enrichment.router,
    spend.router,
    brief.router,
    events.router,
    threads.router,
    cursors.router,
    calendar.router,
    analyses.router,
    contradictions.router,
    investigations.router,
    position_shifts.router,
    admin.router,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    container = Container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await container.startup()     # migrate, open pool, seed, wire pipeline
        container.start_background()  # SSE event bus (LISTEN job_events)
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(title="connect", version="0.1.0", lifespan=lifespan)
    app.state.container = container

    # authlib's Starlette client round-trips state/nonce via
    # request.session — keep the cookie distinct from connect_session and
    # short-lived (it carries nothing after the callback). An ephemeral
    # fallback secret only breaks logins in-flight across a restart.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret or secrets.token_urlsafe(32),
        session_cookie="oauth_state",
        max_age=600,
        same_site="lax",
    )
    # Outermost: cross-origin state-changing requests die before anything
    # else runs (api/security.py).
    app.add_middleware(OriginCheckMiddleware,
                       allowed_origins=settings.allowed_origins)

    # Phase D: per-user rate limits — ONE dependency on every business
    # router, declared after get_current_user so the user is the limit key
    # (api/limits.py; SSE exempt, in-memory windows).
    app.state.rate_limiter = RateLimiter(settings)

    # open: compose healthchecks + the login machinery itself + the public
    # social-card image (Instagram's servers fetch it unauthenticated)
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(social.public_router, prefix="/api")
    for router in _AUTHED_ROUTERS:
        app.include_router(router, prefix="/api",
                           dependencies=[Depends(get_current_user),
                                         Depends(rate_limit)])
    return app


# Default ASGI entrypoint (`uvicorn connect.api.main:app`). Construction is
# side-effect-free: all I/O (DB open, migrate, seed, poller) happens in the
# lifespan, so importing this module stays cheap and offline.
app = create_app()
