"""Phase B auth — mocked-Google code flow (token exchange faked, no
network), the invite gate, first-user-becomes-admin, account linking,
sessions (expiry/touch/logout/disable), the dev-login hatch on/off, the
Origin-check middleware, SSE auth, and a table-driven 401 sweep over every
/api route."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from connect.api.main import create_app
from connect.auth.oauth import LoginRejected
from conftest import TEST_USER_EMAIL, login
from dbutil import q1, qv


# --- fake Google ------------------------------------------------------------

class FakeGoogleOAuth:
    """Drop-in for connect.auth.oauth.GoogleOAuth — the token exchange is
    the only network seam, so faking these two methods mocks all of Google.
    """

    def __init__(self):
        self.claims: dict | LoginRejected = {}
        self.redirect_uri: str | None = None

    async def authorize_redirect(self, request, redirect_uri):
        from starlette.responses import RedirectResponse

        self.redirect_uri = redirect_uri
        return RedirectResponse(
            "https://accounts.google.com/o/oauth2/v2/auth?state=fake",
            status_code=302)

    async def exchange(self, request):
        if isinstance(self.claims, LoginRejected):
            raise self.claims
        return self.claims


def claims_for(email: str, *, sub: str | None = None, verified: bool = True,
               name: str = "Test User",
               picture: str = "https://lh3.example/p.png") -> dict:
    return {"sub": sub or f"sub-{email}", "email": email,
            "email_verified": verified, "name": name, "picture": picture}


@pytest.fixture()
def google_env(settings):
    """A signed-out client whose app carries a FAKE Google client."""
    app = create_app(settings)
    with TestClient(app) as client:
        fake = FakeGoogleOAuth()
        app.state.container.google_oauth = fake
        yield client, fake, app.state.container


def google_callback(client, fake, claims):
    """Drive GET /api/auth/callback with the fake exchange primed."""
    fake.claims = claims
    return client.get("/api/auth/callback?code=x&state=fake",
                      follow_redirects=False)


# --- /api/auth/methods --------------------------------------------------------

def test_methods_dev_only(anon_client):
    body = anon_client.get("/api/auth/methods").json()
    assert body == {"google": False, "dev": True}


def test_methods_with_google(google_env):
    client, _, _ = google_env
    body = client.get("/api/auth/methods").json()
    assert body["google"] is True and body["dev"] is True


def test_methods_all_off(settings, pg_database):
    s = settings.model_copy(update={"dev_login_email": None})
    with TestClient(create_app(s)) as client:
        body = client.get("/api/auth/methods").json()
        assert body == {"google": False, "dev": False}


# --- login redirect -------------------------------------------------------------

def test_login_404_when_google_unconfigured(anon_client):
    resp = anon_client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 404


def test_login_redirects_to_google(google_env):
    client, fake, _ = google_env
    resp = client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(
        "https://accounts.google.com/")
    # default redirect_uri derives from the request (override:
    # CONNECT_OAUTH_REDIRECT_URI)
    assert fake.redirect_uri.endswith("/api/auth/callback")


# --- the google callback: first admin, linking, gate ----------------------------

async def test_first_google_user_becomes_admin(google_env, db):
    client, fake, _ = google_env
    resp = google_callback(client, fake, claims_for("alice@example.com"))
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    assert "connect_session" in resp.cookies

    me = client.get("/api/me").json()
    assert me["email"] == "alice@example.com"
    assert me["role"] == "admin"          # first user ever
    assert me["name"] == "Test User"
    assert me["avatar_url"] == "https://lh3.example/p.png"
    assert me["last_login_at"] is not None

    row = await q1(db, "SELECT * FROM app_user WHERE email = %s",
                   "alice@example.com")
    assert row["google_sub"] == "sub-alice@example.com"


async def test_invite_gate_and_admin_invites(google_env, db):
    client, fake, _ = google_env
    login(client)  # dev hatch: first user => admin

    # uninvited second identity -> friendly signed-out page, NO user row
    resp = google_callback(client, fake, claims_for("bob@example.com"))
    assert resp.status_code == 302
    assert resp.headers["location"] == "/signin?error=not_invited"
    assert await q1(db, "SELECT 1 FROM app_user WHERE email = %s",
                    "bob@example.com") is None

    # the admin invites bob (stored lowercased) -> login succeeds as member
    resp = client.post("/api/admin/invites",
                       json={"email": "Bob@Example.com", "note": "friend"})
    assert resp.status_code == 201
    assert resp.json()["email"] == "bob@example.com"
    # duplicate invite -> 409
    assert client.post("/api/admin/invites",
                       json={"email": "bob@example.com"}).status_code == 409

    resp = google_callback(client, fake, claims_for("bob@example.com"))
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    me = client.get("/api/me").json()   # bob's login replaced the cookie
    assert me["email"] == "bob@example.com"
    assert me["role"] == "member"

    # members cannot touch the invite allowlist (admin-only invites)
    assert client.post("/api/admin/invites",
                       json={"email": "carol@example.com"}).status_code == 403
    assert client.get("/api/admin/invites").status_code == 403


async def test_admin_email_bootstrap_creates_admin(settings, pg_database, db):
    s = settings.model_copy(
        update={"admin_emails": ["Root@Example.com"]})
    app = create_app(s)
    with TestClient(app) as client:
        fake = FakeGoogleOAuth()
        app.state.container.google_oauth = fake
        login(client)  # occupy "first user" so the override is what's tested
        resp = google_callback(client, fake, claims_for("root@example.com"))
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert client.get("/api/me").json()["role"] == "admin"


async def test_account_linking_fills_google_sub(google_env, db):
    client, fake, _ = google_env
    # the ETL-style pre-created row: email known, google_sub NULL
    await db.execute(
        "INSERT INTO app_user (google_sub, email, role, created_at)"
        " VALUES (NULL, 'pre@example.com', 'admin', %s)",
        ("2026-01-01T00:00:00.000Z",))
    resp = google_callback(client, fake, claims_for("pre@example.com",
                                                    sub="sub-999"))
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    row = await q1(db, "SELECT * FROM app_user WHERE email = %s",
                   "pre@example.com")
    assert row["google_sub"] == "sub-999"          # linked, not duplicated
    assert row["last_login_at"] is not None        # login tracked
    assert await qv(db, "SELECT count(*) FROM app_user") == 1


def test_unverified_email_rejected(google_env):
    client, fake, _ = google_env
    resp = google_callback(client, fake,
                           claims_for("x@example.com", verified=False))
    assert resp.status_code == 302
    assert resp.headers["location"] == "/signin?error=email_unverified"


def test_oauth_failure_redirects_to_signin(google_env):
    client, fake, _ = google_env
    resp = google_callback(
        client, fake, LoginRejected("oauth_failed", "state mismatch"))
    assert resp.status_code == 302
    assert resp.headers["location"] == "/signin?error=oauth_failed"


async def test_disabled_user_cannot_login_or_use_session(google_env, db):
    client, fake, _ = google_env
    google_callback(client, fake, claims_for("alice@example.com"))
    assert client.get("/api/me").status_code == 200
    await db.execute("UPDATE app_user SET disabled = true"
                     " WHERE email = 'alice@example.com'")
    # live session rejected instantly (server-side sessions: revocation now)
    assert client.get("/api/me").status_code == 401
    # ... and a fresh login is refused
    resp = google_callback(client, fake, claims_for("alice@example.com"))
    assert resp.headers["location"] == "/signin?error=disabled"


# --- dev-login hatch -------------------------------------------------------------

def test_dev_login_first_user_becomes_admin(anon_client):
    me = login(anon_client)
    assert me["email"] == TEST_USER_EMAIL
    assert me["role"] == "admin"
    assert anon_client.get("/api/me").json()["email"] == TEST_USER_EMAIL


async def test_dev_login_ignores_request_body(anon_client, db):
    # the endpoint never reads an email from the request — it cannot
    # register arbitrary users
    resp = anon_client.post("/api/auth/dev-login",
                            json={"email": "evil@example.com"})
    assert resp.status_code == 200
    assert resp.json()["email"] == TEST_USER_EMAIL
    assert await q1(db, "SELECT 1 FROM app_user WHERE email = %s",
                    "evil@example.com") is None


def test_dev_login_refuses_when_unset(settings, pg_database):
    s = settings.model_copy(update={"dev_login_email": None})
    with TestClient(create_app(s)) as client:
        resp = client.post("/api/auth/dev-login")
        assert resp.status_code == 404
        assert client.get("/api/me").status_code == 401


# --- sessions ---------------------------------------------------------------------

async def test_session_expiry(client, db):
    assert client.get("/api/me").status_code == 200
    await db.execute("UPDATE user_session SET expires_at = %s",
                     ("2020-01-01T00:00:00.000Z",))
    assert client.get("/api/me").status_code == 401
    # the expired row was garbage-collected on rejection
    assert await qv(db, "SELECT count(*) FROM user_session") == 0


async def test_session_sliding_touch(client, db):
    """A request on a stale session extends expires_at (sliding 30d) but
    never past created_at + 90d (hard cap)."""
    await db.execute(
        "UPDATE user_session SET last_seen_at = '2026-01-01T00:00:00.000Z',"
        " expires_at = '2099-01-01T00:00:00.000Z'")
    before = await q1(db, "SELECT * FROM user_session")
    assert client.get("/api/me").status_code == 200
    after = await q1(db, "SELECT * FROM user_session")
    assert after["last_seen_at"] > before["last_seen_at"]
    assert after["expires_at"] < before["expires_at"]        # re-anchored
    assert after["expires_at"] > after["last_seen_at"]
    # cap: expires_at <= created_at + 90d  (string math is fine to the day)
    assert after["expires_at"][:4] != "2099"


async def test_logout(client, db):
    assert await qv(db, "SELECT count(*) FROM user_session") == 1
    assert client.post("/api/auth/logout").status_code == 204
    assert await qv(db, "SELECT count(*) FROM user_session") == 0
    assert client.get("/api/me").status_code == 401
    # idempotent — logging out signed-out is fine
    assert client.post("/api/auth/logout").status_code == 204


def test_garbage_cookie_is_401(anon_client):
    anon_client.cookies.set("connect_session", "not-a-real-session")
    assert anon_client.get("/api/me").status_code == 401


# --- whole-surface 401 sweep --------------------------------------------------------

# /api/social/card/{sha}.jpg is intentionally public — Instagram's servers
# fetch the rendered card image unauthenticated (served only from the
# dedicated card store, keyed by an unguessable content sha).
_OPEN_PATHS = {"/api/health", "/api/social/card/{sha}.jpg"}
_PARAM_VALUES = {"email": "x@example.com", "brief_date": "2026-01-01",
                 "topic": "markets"}


def _fill(path: str) -> str:
    return re.sub(
        r"\{([^}:]+)[^}]*\}",
        lambda m: _PARAM_VALUES.get(m.group(1), "1"),
        path)


def test_every_router_requires_auth(anon_client):
    """Table-driven: every /api route except /api/health and /api/auth/*
    answers 401 JSON to a signed-out request — auth-dependency omissions
    on future routers fail here."""
    from fastapi.routing import APIRoute

    routes = [r for r in anon_client.app.routes if isinstance(r, APIRoute)]
    checked = 0
    failures = []
    for route in routes:
        if not route.path.startswith("/api"):
            continue
        if route.path in _OPEN_PATHS or route.path.startswith("/api/auth/"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            resp = anon_client.request(method, _fill(route.path))
            checked += 1
            if resp.status_code != 401:
                failures.append(
                    f"{method} {route.path} -> {resp.status_code}")
            elif resp.json().get("detail") != "not authenticated":
                failures.append(f"{method} {route.path} -> wrong body")
    assert not failures, failures
    assert checked > 50    # the sweep actually swept the surface


def test_health_stays_open(anon_client):
    assert anon_client.get("/api/health").json()["ok"] is True


# --- SSE auth -------------------------------------------------------------------------

def test_sse_requires_auth_and_cookie_works(client, anon_client):
    # signed out: 401 before any stream starts
    resp = anon_client.get("/api/analyses/1/events")
    assert resp.status_code == 401
    resp = anon_client.get("/api/investigations/1/events")
    assert resp.status_code == 401
    # signed in (cookie auth — exactly what EventSource sends same-origin):
    # past the auth gate, into the route's own 404
    resp = client.get("/api/analyses/1/events")
    assert resp.status_code == 404
    resp = client.get("/api/investigations/1/events")
    assert resp.status_code == 404


# --- Origin-check middleware -------------------------------------------------------------

def test_origin_check_blocks_cross_site_writes(client):
    resp = client.post("/api/watches",
                       json={"kind": "topic", "label": "x",
                             "query_fts": "x"},
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "cross-origin request blocked"
    # 'null' Origin (sandboxed iframes, file://) is cross-site too
    resp = client.post("/api/auth/logout", headers={"Origin": "null"})
    assert resp.status_code == 403


def test_origin_check_allows_same_origin_and_allowlist(client):
    # same-origin: Origin authority == Host (TestClient host: testserver)
    resp = client.post("/api/watches",
                       json={"kind": "topic", "label": "x",
                             "query_fts": "x"},
                       headers={"Origin": "http://testserver"})
    assert resp.status_code == 201
    # allowlisted dev-rewrite origin (settings.allowed_origins default)
    resp = client.post("/api/watches",
                       json={"kind": "topic", "label": "y",
                             "query_fts": "y"},
                       headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 201


def test_origin_check_ignores_safe_methods_and_absent_header(client):
    # cross-origin GETs are fine (no CSRF surface) ...
    resp = client.get("/api/watches",
                      headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    # ... and non-browser clients (no Origin header) pass untouched —
    # every other test in this suite is the proof, but be explicit:
    assert client.post("/api/auth/logout").status_code == 204
