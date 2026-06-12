"""Origin-check middleware — CSRF defense-in-depth (tenancy design §3).

SameSite=Lax on the session cookie already blocks cookie-bearing cross-site
POSTs; this is the second layer: any state-changing request (POST/PUT/
PATCH/DELETE) that carries an Origin header must match either the request's
own host (same-origin: direct hits and the Caddy topology, where the proxy
preserves Host) or the configured allowlist (the Next dev rewrite forwards
the browser's :3000 Origin while the backend sees its own Host).

Requests WITHOUT an Origin header pass: non-browser clients (curl, tests,
server-to-server) cannot ride a victim's cookies, and every modern browser
sends Origin on cross-site state-changing requests. Pure ASGI — no
BaseHTTPMiddleware buffering in the SSE path.
"""

from __future__ import annotations

import json
from typing import Iterable
from urllib.parse import urlsplit

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_FORBIDDEN_BODY = json.dumps(
    {"detail": "cross-origin request blocked"}).encode()


class OriginCheckMiddleware:
    def __init__(self, app, allowed_origins: Iterable[str] = ()):
        self.app = app
        # normalized scheme://host[:port], lowercased
        self.allowed = {o.strip().rstrip("/").lower()
                        for o in allowed_origins if o.strip()}

    def _origin_ok(self, origin: str, host: str | None) -> bool:
        origin = origin.strip().rstrip("/").lower()
        if not origin or origin == "null":
            return False
        if origin in self.allowed:
            return True
        # same-origin: the Origin's authority equals the request's Host
        return host is not None and urlsplit(origin).netloc == host.lower()

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http"
                or scope.get("method") not in _UNSAFE_METHODS):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin")
        if origin is None:
            await self.app(scope, receive, send)
            return
        host = headers.get(b"host")
        if self._origin_ok(origin.decode("latin-1"),
                           host.decode("latin-1") if host else None):
            await self.app(scope, receive, send)
            return
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body",
                    "body": _FORBIDDEN_BODY})
