"""PostgreSQL connection core: async pool factory, type adaptation, bootstrap.

Replaced storage/db.py at port phase P2 (the v0.1 SQLite connection core is
now a dead reference file under connect/tools/legacy_sqlite/).

Type adaptation (design v02-postgres-port.md §1–2):

- **timestamptz -> ISO-string loader.** The frozen Pydantic contracts in
  domain/models.py declare timestamps as ``str`` and dozens of queries
  compare ISO strings. Columns are real ``timestamptz`` in the database, but
  every read comes back as the canonical ``YYYY-MM-DDTHH:MM:SS.mmmZ`` string
  (exactly ``utc_now()``'s shape) — zero churn in contracts or row mappers.
  Writes keep passing ISO strings: psycopg sends ``str`` params with an
  *unspecified* type OID, so Postgres resolves them against the target
  column — text-in, typed-storage, string-out.
- **date -> 'YYYY-MM-DD' loader.** Same trick for the day-precision columns
  (brief_date, source_stats.day, event.occurred_on/window_*, calendar);
  v0.1 stored 10-char strings and sliced/compared them as such.
- **jsonb**: reads come back as parsed Python objects; writes wrap dicts/
  lists in ``Jsonb`` (re-exported here so DAOs import one module).
- **vector(384)**: the pgvector adapter makes ``list[float]`` round-trip —
  ``struct.pack`` dies with the BLOB columns.

``utc_now()`` survives verbatim from db.py — still the one clock, still
returns the ISO string.

Bootstrap: ``init_db(dsn)`` opens its OWN plain connection (never the pool:
on a fresh database the ``vector`` type does not exist until the schema's
``CREATE EXTENSION`` runs, and pool connections register the pgvector
adapter in their configure hook) and runs the advisory-lock-guarded
create-or-migrate in storage/migrations.py. Call order at startup is
therefore: ``await init_db(dsn)``, then ``pool = create_pool(dsn)``,
``await pool.open()``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.datetime import (
    DateBinaryLoader,
    DateLoader,
    TimestamptzBinaryLoader,
    TimestamptzLoader,
)
from pgvector import Vector  # noqa: F401  (re-export for DAOs)
from psycopg.types.json import Json, Jsonb  # noqa: F401  (re-export for DAOs)
from psycopg_pool import AsyncConnectionPool

from connect.storage import migrations

__all__ = [
    "Json",
    "Jsonb",
    "Vector",
    "configure_connection",
    "connect",
    "create_pool",
    "init_db",
    "redact_dsn",
    "register_iso_loaders",
    "utc_now",
]


def utc_now() -> str:
    """ISO-8601 UTC timestamp with milliseconds precision — the one clock."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def redact_dsn(dsn: str) -> str:
    """The DSN with any password replaced by ``***`` — for anything
    user-facing (/api/health's ``db_path`` carried the v0.1 file path; the
    PG DSN must not leak credentials through an unauthenticated endpoint)
    and for logs. Handles both URL DSNs (``postgresql://u:p@h/db``) and
    keyword DSNs (``password=...``)."""
    redacted = re.sub(r"://([^:/@?]+):[^@]*@", r"://\1:***@", dsn)
    return re.sub(r"(password\s*=\s*)\S+", r"\1***", redacted)


def _iso(dt: datetime) -> str:
    """Render a tz-aware datetime in ``utc_now()``'s exact shape."""
    return (dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z")


class _TimestamptzIsoLoader(TimestamptzLoader):
    """timestamptz (text wire format) -> canonical ISO string."""

    def load(self, data: object) -> str:  # type: ignore[override]
        return _iso(super().load(data))  # type: ignore[arg-type]


class _TimestamptzBinaryIsoLoader(TimestamptzBinaryLoader):
    """timestamptz (binary wire format) -> canonical ISO string."""

    def load(self, data: object) -> str:  # type: ignore[override]
        return _iso(super().load(data))  # type: ignore[arg-type]


class _DateIsoLoader(DateLoader):
    """date (text wire format) -> 'YYYY-MM-DD' string."""

    def load(self, data: object) -> str:  # type: ignore[override]
        d: date = super().load(data)  # type: ignore[assignment]
        return d.isoformat()


class _DateBinaryIsoLoader(DateBinaryLoader):
    """date (binary wire format) -> 'YYYY-MM-DD' string."""

    def load(self, data: object) -> str:  # type: ignore[override]
        d: date = super().load(data)  # type: ignore[assignment]
        return d.isoformat()


def register_iso_loaders(
        ctx: psycopg.AsyncConnection | psycopg.Connection) -> None:
    """Register the timestamptz/date -> ISO-string loaders on a connection.

    Registered per-connection (not globally) so non-connect consumers of
    psycopg in the same process are unaffected.
    """
    ctx.adapters.register_loader("timestamptz", _TimestamptzIsoLoader)
    ctx.adapters.register_loader("timestamptz", _TimestamptzBinaryIsoLoader)
    ctx.adapters.register_loader("date", _DateIsoLoader)
    ctx.adapters.register_loader("date", _DateBinaryIsoLoader)


async def configure_connection(conn: psycopg.AsyncConnection) -> None:
    """Per-connection setup: autocommit, UTC, dict rows, ISO loaders,
    pgvector adapter.

    ``dict_row`` keeps ``row["col"]`` working exactly like ``sqlite3.Row``.
    Requires the ``vector`` extension to exist (i.e. ``init_db`` ran first).

    **autocommit=True is deliberate** (P2 decision): it makes
    ``async with conn.transaction():`` byte-for-byte equivalent to sqlite3's
    ``with conn:`` DAO idiom — BEGIN…COMMIT around the block, per-statement
    autocommit otherwise. Under the non-autocommit default, an earlier read
    on a long-held connection silently opens an implicit transaction, every
    later ``conn.transaction()`` degrades to a SAVEPOINT, and "committed"
    DAO writes stay invisible to other connections until something finally
    commits — exactly the corruption-by-default the port must not have.
    Nested ``transaction()`` blocks still become SAVEPOINTs (design §2).

    **session TimeZone = UTC**: naive ISO-string parameters (the v0.1 write
    shape) must be interpreted as UTC when coerced into timestamptz, the
    exact semantics of v0.1's lexicographic TEXT comparisons.
    """
    from pgvector.psycopg import register_vector_async

    await conn.set_autocommit(True)
    await conn.execute("SET TIMEZONE TO 'UTC'")
    conn.row_factory = dict_row  # type: ignore[assignment]
    register_iso_loaders(conn)
    await register_vector_async(conn)


def create_pool(dsn: str, *, min_size: int = 2,
                max_size: int = 10) -> AsyncConnectionPool:
    """Async pool factory; returned closed — caller ``await pool.open()``.

    Sizing (design §2): API replica min=2/max=10; worker replica
    max = job_concurrency + 3 (jobs + poller + governor/spend reads).
    SSE endpoints must never hold a pooled connection across the stream —
    acquire per poll tick.
    """
    return AsyncConnectionPool(
        dsn, min_size=min_size, max_size=max_size, open=False,
        configure=configure_connection)


async def connect(dsn: str) -> psycopg.AsyncConnection:
    """One fully configured standalone connection (tests, tools, ETL-side).

    Same setup as a pooled connection; caller owns close().
    """
    conn = await psycopg.AsyncConnection.connect(dsn)
    await configure_connection(conn)
    return conn


async def init_db(dsn: str, *, lock_timeout_s: Optional[float] = None) -> int:
    """Create the schema on a fresh DB, or migrate an existing one forward.

    Opens a dedicated PLAIN connection (see module docstring for why not the
    pool) and delegates to the advisory-lock-guarded
    ``migrations.pg_migrate``. Returns the resulting schema version; refuses
    a DB newer than this code (StorageVersionError) — never silently
    corrupts. ``lock_timeout_s`` bounds the wait on the advisory lock
    (default: wait forever, matching replica-startup semantics).
    """
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        if lock_timeout_s is not None:
            # SET takes no bind params; set_config is the parameterized form.
            await conn.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{int(lock_timeout_s * 1000)}ms",))
        return await migrations.pg_migrate(conn)
