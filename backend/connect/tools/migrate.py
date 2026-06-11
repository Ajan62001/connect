"""One-shot migration entrypoint — ``python -m connect.tools.migrate``.

The compose ``migrate`` service (runtime design §7) runs this once per
``docker compose up``; api/worker containers gate their start on its
success (``service_completed_successfully``). Migrations are single-flight
twice over: this service is the only thing racing nothing, and
``init_db`` itself serializes under ``pg_advisory_xact_lock`` — even
racing manual starts (or the api/worker containers' own ``init_db`` at
startup) cannot corrupt.

Exit code 0 = schema is at the code's version (created fresh or migrated
forward). Nonzero = unreachable database, a newer-than-code schema
(StorageVersionError — never silently corrupt with an older binary), or a
failed migration (transactional DDL: nothing half-applied).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from connect.orchestration.config import Settings
from connect.storage import pg


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("connect.tools.migrate")
    settings = Settings()
    try:
        version = asyncio.run(pg.init_db(settings.database_url))
    except Exception:  # noqa: BLE001 — entrypoint boundary: report + exit 1
        log.exception("migration failed (database_url=%s)",
                      pg.redact_dsn(settings.database_url))
        return 1
    log.info("schema at v%s (database_url=%s)", version,
             pg.redact_dsn(settings.database_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
