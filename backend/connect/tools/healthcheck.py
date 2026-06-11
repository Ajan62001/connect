"""Container healthcheck — ``python -m connect.tools.healthcheck [--worker]``.

Compose healthchecks (runtime design §7) exec this inside the container:

- **api** (no flag): GET ``http://127.0.0.1:8000/api/health`` and require
  ``{"ok": true}``. Exercises the full path users depend on — uvicorn up,
  pool acquirable, schema stamped.
- **worker** (``--worker``): the worker exposes no HTTP, so the honest
  cheap check is environmental: open one DB connection, ``SELECT 1``, and
  confirm the schema version table answers. A wedged-but-alive worker
  coroutine is NOT detected here — that is the beat leader's job (stale
  heartbeats orphan its jobs and requeue them, runtime design §2), so the
  healthcheck deliberately stays a liveness probe, not a work probe.

stdlib-only on the api path (urllib): the check must not depend on the app
import graph being healthy to report that the app is unhealthy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def check_api(port: int, timeout: float) -> int:
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — healthcheck boundary
        print(f"unhealthy: {url}: {exc}", file=sys.stderr)
        return 1
    if body.get("ok") is not True:
        print(f"unhealthy: {url}: ok={body.get('ok')!r}", file=sys.stderr)
        return 1
    return 0


def check_worker(timeout: float) -> int:
    # Imports inside: psycopg/Settings only load on the worker path.
    import psycopg

    from connect.orchestration.config import Settings

    dsn = Settings().database_url
    try:
        with psycopg.connect(dsn, connect_timeout=int(timeout)) as conn:
            conn.execute("SELECT 1")
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            print("unhealthy: schema_version missing", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001 — healthcheck boundary
        print(f"unhealthy: db: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true",
                        help="worker-flavor check (DB liveness, no HTTP)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("CONNECT_API_PORT",
                                                   "8000")),
                        help="api port inside the container (default 8000)")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    if args.worker:
        return check_worker(args.timeout)
    return check_api(args.port, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
