"""Interactive backpressure + per-user rate limiting (runtime design §5,
tenancy design §4 — Phase D).

Backpressure: before accepting POST /analyses | /investigations the route
checks (a) the caller's in-flight interactive jobs against
``CONNECT_USER_MAX_INTERACTIVE`` and (b) the global interactive queue depth
against ``CONNECT_INTERACTIVE_QUEUE_LIMIT``. Over either → HTTP 429 with
``Retry-After: 60`` and a friendly detail. Cheap COUNTs on the partial
claim index — the queued/running rows are exactly what they cover.

Rate limiting: the design picked slowapi, which is a thin wrapper over the
``limits`` engine; we drive ``limits`` directly through ONE router-level
FastAPI dependency instead because (a) slowapi's middleware runs before
routing/auth so it cannot key on the authenticated user, and (b) a
decorator per route across ~40 read endpoints fights the router-level
``Depends(get_current_user)`` pattern. Same engine, same storage model:
in-memory moving windows, per-process — restarts/replicas reset them
(documented risk; the PG-backed governors remain the hard cost backstop),
and a Redis storage URI swaps in without code changes if replicas appear.

Buckets (design §4 defaults): 120/min reads + other mutations, 10/min
ingest (POST /api/documents), 5/min analysis/investigation creation; SSE
endpoints (…/events) exempt — they are long-lived, not chatty.
"""

from __future__ import annotations

import psycopg
from fastapi import HTTPException, Request
from limits import RateLimitItem, RateLimitItemPerMinute
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

from connect.auth.sessions import SESSION_COOKIE
from connect.orchestration.config import Settings

INTERACTIVE_KINDS: tuple[str, ...] = ("analysis", "investigation")

RETRY_AFTER_S = 60


def _too_many(detail: str) -> HTTPException:
    return HTTPException(status_code=429, detail=detail,
                         headers={"Retry-After": str(RETRY_AFTER_S)})


async def check_interactive_capacity(conn: psycopg.AsyncConnection,
                                     settings: Settings,
                                     user_id: int) -> None:
    """Raise 429 when the caller or the deployment is at interactive
    capacity (runtime design §5). Call BEFORE creating the dossier row."""
    cur = await conn.execute(
        "SELECT count(*) AS n FROM job"
        " WHERE owner_id = %s AND kind = ANY(%s)"
        " AND status IN ('queued', 'running')",
        (user_id, list(INTERACTIVE_KINDS)))
    mine = int((await cur.fetchone())["n"])
    if mine >= settings.user_max_interactive:
        raise _too_many(
            f"you already have {mine} analyses/investigations in flight"
            f" (limit {settings.user_max_interactive}) — wait for one to"
            f" finish, or cancel it")
    cur = await conn.execute(
        "SELECT count(*) AS n FROM job"
        " WHERE kind = ANY(%s) AND status = 'queued'",
        (list(INTERACTIVE_KINDS),))
    queued = int((await cur.fetchone())["n"])
    if queued >= settings.interactive_queue_limit:
        raise _too_many(
            "the system is busy verifying other claims — try again in a"
            " minute")


class RateLimiter:
    """Per-user moving-window request limits over the ``limits`` engine."""

    def __init__(self, settings: Settings):
        self.enabled = settings.rate_limit_enabled
        self._strategy = MovingWindowRateLimiter(MemoryStorage())
        self._read = RateLimitItemPerMinute(
            settings.rate_limit_read_per_minute)
        self._ingest = RateLimitItemPerMinute(
            settings.rate_limit_ingest_per_minute)
        self._create = RateLimitItemPerMinute(
            settings.rate_limit_create_per_minute)

    def classify(self, method: str, path: str) -> tuple[str,
                                                        RateLimitItem] | None:
        """(bucket-name, limit) for a request; None = exempt (SSE)."""
        if path.endswith("/events"):
            return None  # long-lived SSE streams are not request traffic
        if method in ("GET", "HEAD", "OPTIONS"):
            return ("read", self._read)
        if (path in ("/api/analyses", "/api/investigations")
                or (path.startswith("/api/questions/")
                    and path.endswith("/investigate"))):
            return ("create", self._create)
        if path == "/api/documents":
            return ("ingest", self._ingest)
        return ("read", self._read)  # other mutations share the default

    async def hit(self, bucket: str, item: RateLimitItem,
                  key: str) -> bool:
        return await self._strategy.hit(item, bucket, key)


async def rate_limit(request: Request) -> None:
    """Router-level dependency (declared AFTER get_current_user, so the
    authenticated user is the limit key; anonymous fallbacks — the session
    cookie, else the client address — only matter for 401 paths)."""
    limiter: RateLimiter | None = getattr(request.app.state,
                                          "rate_limiter", None)
    if limiter is None or not limiter.enabled:
        return
    classified = limiter.classify(request.method, request.url.path)
    if classified is None:
        return
    bucket, item = classified
    user = getattr(request.state, "current_user", None)
    if user is not None:
        key = f"u:{user.id}"
    else:
        key = ("s:" + request.cookies.get(SESSION_COOKIE, "")
               if request.cookies.get(SESSION_COOKIE)
               else "ip:" + (request.client.host if request.client
                             else "unknown"))
    if not await limiter.hit(bucket, item, key):
        raise _too_many(
            f"rate limit exceeded ({item.amount} {bucket} requests per"
            f" minute) — slow down and try again shortly")
