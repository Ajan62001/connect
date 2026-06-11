"""Polite async HTTP fetcher.

- per-domain politeness: 1 request / 2 s (configurable). v0.2: when a pool
  is bound (``bind_pool``), the slot is an atomic PG reservation in
  fetch_domain — exact politeness across every api/worker process (runtime
  design §4); unbound (standalone tools/tests) it falls back to the v0.1
  in-process bucket.
- robots.txt cache with TTL; robots failures => allow (NIC sites 403 bots).
  v0.2: the body cache is the shared robots_cache table (1h TTL) behind a
  small in-process cache (5 min) so hot domains don't query per fetch;
  parsing stays local.
- browser User-Agent — NIC .gov.in properties 403 non-browser UAs (verified)
- 30 s timeout, 10 MB cap (streamed), 2 retries with backoff on transport
  errors / 5xx
- http:// failures retried once as https:// — stale feeds carry http links
  to sites that dropped plain-HTTP (RBI hard-404s http, verified June 2026)
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
from psycopg_pool import AsyncConnectionPool

from connect.storage import fetchstate

log = logging.getLogger(__name__)

# A current-ish Chrome UA; NIC .gov.in servers 403 obvious bot UAs.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_ROBOTS_TTL = 3600.0        # shared robots_cache row TTL (seconds)
_ROBOTS_LOCAL_TTL = 300.0   # in-process parser cache on top of the row


class FetchError(Exception):
    """Terminal fetch failure (after retries) — callers map to HTTP 502."""


class FetchDisallowed(FetchError):
    """robots.txt disallows this URL for our UA."""


class FetchTooLarge(FetchError):
    """Response body exceeded the byte cap."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content: bytes
    content_type: str


class Fetcher:
    def __init__(self, *, user_agent: str = BROWSER_UA,
                 per_domain_interval: float = 2.0,
                 timeout_seconds: float = 30.0,
                 max_bytes: int = 10 * 1024 * 1024,
                 retries: int = 2,
                 respect_robots: bool = True):
        self.user_agent = user_agent
        self.per_domain_interval = per_domain_interval
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.retries = retries
        self.respect_robots = respect_robots
        self._client: httpx.AsyncClient | None = None
        self._pool: AsyncConnectionPool | None = None
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_next: dict[str, float] = {}
        self._robots: dict[str, tuple[urllib.robotparser.RobotFileParser | None, float]] = {}

    def bind_pool(self, pool: AsyncConnectionPool) -> None:
        """Switch politeness state to the shared PG backing (called by the
        composition root once the pool is open). The Fetcher API is
        unchanged — same seam, new backing."""
        self._pool = pool

    # -- lifecycle -----------------------------------------------------------

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;"
                              "q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-IN,en;q=0.9",
                },
                follow_redirects=True,
                timeout=self.timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- politeness ------------------------------------------------------------

    async def _throttle(self, domain: str) -> None:
        if self._pool is not None:
            # one cheap autocommit upsert reserves this process's slot;
            # the connection is returned BEFORE the local sleep (a
            # reservation must never sit in a transaction or pin the pool)
            async with self._pool.connection() as conn:
                wait_s = await fetchstate.reserve_slot(
                    conn, domain, self.per_domain_interval)
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            return
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            next_at = self._domain_next.get(domain, 0.0)
            if next_at > now:
                await asyncio.sleep(next_at - now)
            self._domain_next[domain] = time.monotonic() + self.per_domain_interval

    async def _fetch_robots_body(self, base: str) -> str | None:
        """GET robots.txt; None = unreachable/non-200 (=> allow)."""
        try:
            resp = await self._client_or_create().get(
                base + "/robots.txt", timeout=10.0)
            if resp.status_code == 200:
                return resp.text
        except httpx.HTTPError:
            pass
        return None

    @staticmethod
    def _parse_robots(
            body: str | None) -> urllib.robotparser.RobotFileParser | None:
        if body is None:
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.splitlines())
        return parser

    async def _robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(base)
        now = time.monotonic()
        if cached is None or cached[1] < now:
            if self._pool is not None:
                # read-through the SHARED cache: row fresh -> parse its
                # body locally; stale/missing -> fetch once, upsert for
                # every other process
                async with self._pool.connection() as conn:
                    row = await fetchstate.get_robots(conn, base,
                                                      ttl_s=_ROBOTS_TTL)
                if row is not None and row["fresh"]:
                    body = row["body"]
                else:
                    body = await self._fetch_robots_body(base)
                    async with self._pool.connection() as conn:
                        await fetchstate.upsert_robots(conn, base, body)
                parser = self._parse_robots(body)
                self._robots[base] = (parser, now + _ROBOTS_LOCAL_TTL)
            else:
                parser = self._parse_robots(
                    await self._fetch_robots_body(base))
                self._robots[base] = (parser, now + _ROBOTS_TTL)
            cached = self._robots[base]
        parser = cached[0]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    # -- fetch -----------------------------------------------------------------

    async def fetch(self, url: str, *,
                    ignore_robots: bool = False) -> FetchResult:
        """Fetch a URL politely.

        ``ignore_robots=True`` is for user-registered endpoints (e.g. a
        source's own feed_url): the user explicitly subscribed, so this is a
        feed-reader request, not autonomous crawling. Discovered links keep
        full robots enforcement.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError(f"unsupported URL scheme: {url!r}")
        try:
            return await self._fetch_with_retries(
                url, parts.netloc, ignore_robots=ignore_robots)
        except (FetchTooLarge, FetchDisallowed):
            raise  # https twin would be just as large / just as disallowed
        except FetchError:
            if parts.scheme != "http":
                raise
            # Feed-drift fallback: stale feeds carry http:// links to sites
            # that now serve only https (RBI 404s plain http — verified).
            https_url = urlunsplit(("https", *parts[1:]))
            log.info("http fetch failed; retrying as %s", https_url)
            return await self._fetch_with_retries(
                https_url, parts.netloc, ignore_robots=ignore_robots)

    async def _fetch_with_retries(self, url: str, domain: str, *,
                                  ignore_robots: bool = False) -> FetchResult:
        if not ignore_robots and not await self._robots_allowed(url):
            raise FetchDisallowed(f"robots.txt disallows {url}")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            await self._throttle(domain)
            try:
                return await self._fetch_once(url)
            except (FetchTooLarge, FetchDisallowed, FetchTerminal):
                raise
            except (httpx.HTTPError, FetchError) as e:
                last_error = e
                if attempt < self.retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise FetchError(f"fetch failed for {url}: {last_error}") from last_error

    async def _fetch_once(self, url: str) -> FetchResult:
        client = self._client_or_create()
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 500:
                raise FetchError(f"server error {resp.status_code}")
            if resp.status_code >= 400:
                # 4xx is terminal — retrying won't help
                raise FetchTerminal(url, resp.status_code)
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    raise FetchTooLarge(
                        f"{url} exceeded {self.max_bytes} bytes")
                chunks.append(chunk)
            return FetchResult(
                url=url,
                final_url=str(resp.url),
                status_code=resp.status_code,
                content=b"".join(chunks),
                content_type=resp.headers.get("content-type", ""),
            )


class FetchTerminal(FetchError):
    """4xx — do not retry."""

    def __init__(self, url: str, status_code: int):
        super().__init__(f"HTTP {status_code} for {url}")
        self.status_code = status_code
