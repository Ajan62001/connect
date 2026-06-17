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
import ipaddress
import logging
import socket
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from psycopg_pool import AsyncConnectionPool

from connect.storage import fetchstate

log = logging.getLogger(__name__)

# A current-ish Chrome UA; NIC .gov.in servers 403 obvious bot UAs.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# curl_cffi impersonation profile used for the 403 fallback (see
# _impersonated_fetch). Some CDNs (Akamai bot-defense on Moneycontrol,
# Business Standard) block on the TLS/JA3 fingerprint of a plain httpx
# client regardless of the User-Agent; replaying a real Chrome handshake
# clears them. Verified June 2026.
_IMPERSONATE_PROFILE = "chrome"

_ROBOTS_TTL = 3600.0        # shared robots_cache row TTL (seconds)
_ROBOTS_LOCAL_TTL = 300.0   # in-process parser cache on top of the row


class FetchError(Exception):
    """Terminal fetch failure (after retries) — callers map to HTTP 502."""


class FetchDisallowed(FetchError):
    """robots.txt disallows this URL for our UA."""


class FetchTooLarge(FetchError):
    """Response body exceeded the byte cap."""


class FetchBlocked(FetchError):
    """SSRF guard: the host resolves to a private/loopback/reserved address.
    Terminal — never retried, never tried as an https twin."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content: bytes
    content_type: str


def _ip_is_blocked(ip_text: str) -> bool:
    """True for addresses we must never fetch server-side: RFC1918 private,
    loopback (127/8, ::1), link-local (169.254/16 incl. the cloud metadata
    endpoint, fe80::/10), unique-local (fc00::/7), reserved, multicast,
    unspecified (0.0.0.0)."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _host_is_blocked(host: str | None) -> bool:
    """Resolve ``host`` and block if ANY resolved address is internal. A
    bare IP literal is classified directly; an unresolvable name is NOT
    blocked here (the connection simply fails downstream)."""
    if not host:
        return True
    h = host.strip("[]")  # IPv6 literal arrives bracketed from a URL
    try:
        ipaddress.ip_address(h)
        return _ip_is_blocked(h)           # literal address
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            h, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False  # let the real connection attempt fail normally
    return any(_ip_is_blocked(info[4][0]) for info in infos)


class _SsrfGuardTransport(httpx.AsyncHTTPTransport):
    """Validates EVERY request the client makes — including each redirect hop,
    since httpx invokes the transport per hop — before the socket opens, so an
    internal target is refused rather than fetched. The single chokepoint
    behind every Fetcher.fetch (polls, link-follow, user-URL ingest,
    investigation fetch, backfill)."""

    async def handle_async_request(self, request: httpx.Request
                                   ) -> httpx.Response:
        if await _host_is_blocked(request.url.host):
            raise FetchBlocked(
                f"refusing to fetch internal/reserved host: "
                f"{request.url.host!r}")
        return await super().handle_async_request(request)


class Fetcher:
    def __init__(self, *, user_agent: str = BROWSER_UA,
                 per_domain_interval: float = 2.0,
                 timeout_seconds: float = 30.0,
                 max_bytes: int = 10 * 1024 * 1024,
                 retries: int = 2,
                 respect_robots: bool = True,
                 impersonate_on_403: bool = True):
        self.user_agent = user_agent
        self.per_domain_interval = per_domain_interval
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.retries = retries
        self.respect_robots = respect_robots
        self.impersonate_on_403 = impersonate_on_403
        self._client: httpx.AsyncClient | None = None
        self._imp_session = None  # lazy curl_cffi AsyncSession for 403 fallback
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
                # SSRF chokepoint: every hop (incl. redirects) is validated
                # against private/reserved address space before connecting.
                transport=_SsrfGuardTransport(),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._imp_session is not None:
            await self._imp_session.close()
            self._imp_session = None

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
        except (FetchTooLarge, FetchDisallowed, FetchBlocked):
            raise  # the https twin is just as large / disallowed / internal
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
            except (FetchTooLarge, FetchDisallowed, FetchTerminal,
                    FetchBlocked):
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
            if resp.status_code == 403 and self.impersonate_on_403:
                # Likely a CDN bot-defence block on our TLS fingerprint, not a
                # real authz denial — replay a genuine Chrome handshake before
                # giving up (Akamai on Moneycontrol / Business Standard).
                imp = await self._impersonated_fetch(url)
                if imp is not None:
                    return imp
                raise FetchTerminal(url, 403)
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

    async def _impersonated_fetch(self, url: str) -> FetchResult | None:
        """403 fallback: re-fetch ``url`` replaying a real Chrome TLS/JA3
        fingerprint via curl_cffi, which clears CDN bot-defence blocks that
        key on the handshake rather than the User-Agent.

        Returns a FetchResult on a clean <400 response, or None if the block
        persists / the fallback is unavailable — the caller then surfaces the
        original 403. SSRF discipline is preserved: redirects are followed
        manually with a private/reserved host check on every hop (curl_cffi
        bypasses the httpx SsrfGuardTransport), and the byte cap is enforced
        on the materialised body.
        """
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            log.warning("curl_cffi not installed; cannot retry 403 for %s", url)
            return None

        if self._imp_session is None:
            self._imp_session = AsyncSession()

        current = url
        for _ in range(4):  # cap redirect hops, like a browser would
            if await _host_is_blocked(urlsplit(current).hostname):
                raise FetchBlocked(
                    f"refusing to fetch internal/reserved host: "
                    f"{urlsplit(current).hostname!r}")
            try:
                resp = await self._imp_session.get(
                    current,
                    impersonate=_IMPERSONATE_PROFILE,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
            except FetchBlocked:
                raise
            except Exception as e:  # curl_cffi transport/timeout errors
                log.info("impersonated fetch failed for %s: %s", current, e)
                return None

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            if resp.status_code >= 400:
                return None  # block persists — surface the original 403
            if len(resp.content) > self.max_bytes:
                raise FetchTooLarge(
                    f"{current} exceeded {self.max_bytes} bytes")
            log.info("impersonated fetch cleared block for %s", url)
            return FetchResult(
                url=url,
                final_url=current,
                status_code=resp.status_code,
                content=resp.content,
                content_type=resp.headers.get("content-type", ""),
            )
        return None  # too many redirects


class FetchTerminal(FetchError):
    """4xx — do not retry."""

    def __init__(self, url: str, status_code: int):
        super().__init__(f"HTTP {status_code} for {url}")
        self.status_code = status_code
