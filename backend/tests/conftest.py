"""Shared fixtures. NO network, NO model downloads: poller off, embeddings
off — enforced through Settings, not monkeypatching.

PostgreSQL test infrastructure (v0.2 port, design v02-postgres-port.md §6):
Postgres on loopback is blessed local infra — start it explicitly with

    docker compose -f docker-compose.test.yml up -d db-test

PG-backed tests SKIP with a loud message when the database is unreachable.
Fixture layers: session — ensure a `connect_template` database carrying the
current schema (fingerprinted over PG_DDL, rebuilt on DDL change); per
xdist-worker — clone `connect_test_w_{worker}` from the template (~150ms);
per test — `pg_database` hands the worker DB out and TRUNCATEs every table
except meta (RESTART IDENTITY CASCADE) on teardown — the isolation
equivalent of v0.1's fresh tmp_path database. `settings` points
database_url at it, so `container`/`client`/`db` all share one database
per test. `pg_fresh_dsn` hands out an EMPTY database for
init_db/migration-race tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from connect.api.main import create_app
from connect.orchestration.config import Settings
from connect.orchestration.container import Container


# The dev-login escape hatch doubles as the test login: the `client`
# fixture signs in through the REAL auth path (user row + session row +
# cookie), so every API test runs authenticated exactly like production.
TEST_USER_EMAIL = "dev@test.local"


@pytest.fixture()
def settings(tmp_path, pg_database) -> Settings:
    return Settings(
        database_url=pg_database,
        blob_dir=tmp_path / "blobs",
        poller_enabled=False,
        embeddings_enabled=False,
        # never auto-queue LLM work in tests (backend/.env may carry a real
        # ANTHROPIC_API_KEY); tests inject MockProvider explicitly instead
        enrich_fast_path_enabled=False,
        # deterministic suites hammer the API far past 120 req/min; the
        # dedicated rate-limit tests opt back in with their own Settings
        rate_limit_enabled=False,
        # auth: deterministic oauth_state signing + the dev-login hatch
        # (backend/.env may carry real Google credentials — never let them
        # leak into tests)
        session_secret="test-secret",
        dev_login_email=TEST_USER_EMAIL,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        admin_emails=[],
        frontend_origin="",
    )


@pytest.fixture()
async def container(settings) -> Container:
    c = Container(settings)
    await c.startup()
    yield c
    await c.shutdown()


def login(test_client: TestClient) -> dict:
    """Dev-login on this client (cookie persists in its jar); returns the
    /api/me payload. First login in a fresh DB creates the admin user."""
    resp = test_client.post("/api/auth/dev-login")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture()
def client(settings):
    """An AUTHENTICATED TestClient (first user => admin). Tests that need
    the signed-out state use `anon_client` or clear the cookie jar."""
    app = create_app(settings)
    with TestClient(app) as test_client:
        login(test_client)
        yield test_client


@pytest.fixture()
def anon_client(settings):
    """A signed-out TestClient on the same app/database wiring."""
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
async def pool(settings):
    """A small AsyncConnectionPool on the per-test database — for tests
    that construct Governors/queues directly without a Container."""
    from connect.storage import pg as pg_mod

    p = pg_mod.create_pool(settings.database_url, min_size=1, max_size=4)
    await p.open(wait=True)
    yield p
    await p.close()


@pytest.fixture()
async def db(settings):
    """One configured async connection to the SAME database the
    container/client under test uses (autocommit — writes are immediately
    visible across connections, exactly the v0.1 shared-connection
    semantics)."""
    from connect.storage import pg as pg_mod

    conn = await pg_mod.connect(settings.database_url)
    yield conn
    await conn.close()


# --- PostgreSQL test infrastructure (v0.2 port P1) ---------------------------

PG_TEST_DSN = os.environ.get(
    "CONNECT_TEST_DATABASE_URL",
    "postgresql://connect:connect@127.0.0.1:55432/connect_test")

_PG_TEMPLATE = "connect_template"
# Setup coordination key — advisory locks are per-database, so this never
# collides with migrations.PG_MIGRATION_LOCK_KEY (taken in the target DBs).
_PG_SETUP_LOCK_KEY = 0x636F6E6E5F7467


def _pg_db_dsn(name: str) -> str:
    return PG_TEST_DSN.rsplit("/", 1)[0] + "/" + name


def _pg_fingerprint() -> str:
    """Identity of the current DDL — template rebuilt when it changes."""
    from connect.storage import schema

    text = "\n".join(schema.PG_DDL) + f"|v{schema.PG_SCHEMA_VERSION}"
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(scope="session")
def pg_admin():
    """Autocommit maintenance connection; SKIPS PG tests loudly if down."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(PG_TEST_DSN, autocommit=True,
                               connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(
            f"PG test database unreachable at {PG_TEST_DSN} ({e}). "
            "Start it with: docker compose -f docker-compose.test.yml up -d"
            " db-test  (override via CONNECT_TEST_DATABASE_URL)")
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def pg_template(pg_admin) -> str:
    """Ensure `connect_template` exists with the CURRENT schema applied.

    Serialized across (xdist) sessions by an advisory lock; an existing
    template with a stale DDL fingerprint is dropped and rebuilt. Schema
    application goes through pg.init_db — the same code path the app uses,
    never a second DDL source.
    """
    import psycopg

    from connect.storage import pg as pg_mod

    fingerprint = _pg_fingerprint()
    pg_admin.execute("SELECT pg_advisory_lock(%s)", (_PG_SETUP_LOCK_KEY,))
    try:
        exists = pg_admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (_PG_TEMPLATE,)).fetchone() is not None
        current = False
        if exists:
            try:
                with psycopg.connect(_pg_db_dsn(_PG_TEMPLATE)) as tconn:
                    row = tconn.execute(
                        "SELECT value FROM meta"
                        " WHERE key = 'ddl_fingerprint'").fetchone()
                    current = row is not None and row[0] == fingerprint
            except psycopg.Error:
                current = False
            if not current:
                pg_admin.execute(
                    f'DROP DATABASE "{_PG_TEMPLATE}" WITH (FORCE)')
        if not current:
            pg_admin.execute(f'CREATE DATABASE "{_PG_TEMPLATE}"')
            asyncio.run(pg_mod.init_db(_pg_db_dsn(_PG_TEMPLATE)))
            with psycopg.connect(_pg_db_dsn(_PG_TEMPLATE)) as tconn:
                tconn.execute(
                    "INSERT INTO meta (key, value)"
                    " VALUES ('ddl_fingerprint', %s)"
                    " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (fingerprint,))
    finally:
        pg_admin.execute("SELECT pg_advisory_unlock(%s)",
                         (_PG_SETUP_LOCK_KEY,))
    return _PG_TEMPLATE


@pytest.fixture(scope="session")
def pg_db_dsn(pg_admin, pg_template) -> str:
    """Per-(xdist-)worker database cloned from the template."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    name = f"connect_test_w_{worker}"
    # Same lock as the template build: a clone must not race another
    # session's fingerprint probe (template must have no connections).
    pg_admin.execute("SELECT pg_advisory_lock(%s)", (_PG_SETUP_LOCK_KEY,))
    try:
        pg_admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        pg_admin.execute(
            f'CREATE DATABASE "{name}" TEMPLATE "{pg_template}"')
    finally:
        pg_admin.execute("SELECT pg_advisory_unlock(%s)",
                         (_PG_SETUP_LOCK_KEY,))
    return _pg_db_dsn(name)


def _truncate_all(dsn: str) -> None:
    """TRUNCATE every public table except meta, RESTART IDENTITY CASCADE."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename <> 'meta'")]
        if names:
            conn.execute(
                "TRUNCATE " + ", ".join(f'"{n}"' for n in names)
                + " RESTART IDENTITY CASCADE")


@pytest.fixture()
def pg_database(pg_db_dsn) -> str:
    """The per-worker database, truncated clean AFTER each test (it is
    cloned pristine from the template, so the first test starts clean)."""
    yield pg_db_dsn
    _truncate_all(pg_db_dsn)


@pytest.fixture()
async def pg_conn(pg_db_dsn):
    """One configured async connection (dict rows, ISO loaders, pgvector);
    every public table except meta truncated on teardown — the per-test
    isolation equivalent of v0.1's fresh tmp_path database."""
    from connect.storage import pg as pg_mod

    conn = await pg_mod.connect(pg_db_dsn)
    try:
        yield conn
    finally:
        await conn.close()
    _truncate_all(pg_db_dsn)


@pytest.fixture()
def pg_fresh_dsn(pg_admin):
    """An EMPTY database (no schema) for init_db / migration-race tests;
    dropped on teardown."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    name = f"connect_test_fresh_{worker}"
    pg_admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    pg_admin.execute(f'CREATE DATABASE "{name}"')
    yield _pg_db_dsn(name)
    pg_admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
