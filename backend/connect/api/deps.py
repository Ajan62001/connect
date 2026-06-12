"""Request-scoped access to the composition root + a pooled connection +
the auth seam every router consumes (tenancy design §3)."""

from __future__ import annotations

from typing import AsyncIterator

import psycopg
from fastapi import Depends, HTTPException, Request

from connect.auth.sessions import SESSION_COOKIE, resolve
from connect.domain.models import CurrentUser
from connect.orchestration.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container


async def get_db(request: Request) -> AsyncIterator[psycopg.AsyncConnection]:
    """One pooled connection per request, returned at response end.

    SSE endpoints must NOT use this (a stream would pin a pooled
    connection); they acquire per poll tick from container.pool instead.
    """
    container: Container = request.app.state.container
    async with container.pool.connection() as conn:
        yield conn


async def get_current_user(
        request: Request,
        db: psycopg.AsyncConnection = Depends(get_db)) -> CurrentUser:
    """Session cookie -> CurrentUser; 401 JSON on missing/expired/disabled.

    Applied router-wide in main.py — the whole /api surface except /health
    and /auth/* is authenticated. Cookie auth covers SSE too (EventSource
    sends cookies on same-origin requests; design §3). FastAPI caches the
    get_db sub-dependency per request, so this shares the route's pooled
    connection rather than acquiring a second one.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="not authenticated")
    container: Container = request.app.state.container
    user = await resolve(db, session_id, container.settings)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    request.state.current_user = user
    return user


async def require_admin(
        user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user
