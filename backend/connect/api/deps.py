"""Request-scoped access to the composition root + a pooled connection."""

from __future__ import annotations

from typing import AsyncIterator

import psycopg
from fastapi import Request

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
