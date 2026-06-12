"""Google OAuth (authlib code flow) + the login gate policy
(tenancy design §3).

Two halves, deliberately separable:

- ``GoogleOAuth`` — thin authlib wrapper (discovery, PKCE/state/nonce, ID
  token verification all come from authlib's Starlette client). Tests
  replace the whole object with a fake — the token exchange is the ONLY
  network seam.
- ``login_identity`` — the gate + provisioning policy shared by the Google
  callback and the dev-login hatch: lookup by google_sub, then by verified
  email (account linking fills google_sub — also how a pre-created admin
  row activates), else create behind the invite gate; the FIRST user ever
  becomes admin (race-safe via an advisory xact lock); disabled users are
  rejected; last_login_at is stamped on every successful login.

Google is identity-only: scopes ``openid email profile``, access/refresh
tokens are never stored — "refresh" is the sliding app session.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg
from starlette.requests import Request
from starlette.responses import Response

from connect.domain.models import CurrentUser
from connect.orchestration.config import Settings
from connect.storage import invites as invite_dao
from connect.storage import users as user_dao

# Advisory xact lock serializing user creation: two racing first logins must
# not both become "first user ever". ASCII "cnnctusr"-ish, distinct from the
# migration lock key.
USER_CREATE_LOCK_KEY = 0x636E6E63747573


class LoginRejected(Exception):
    """A friendly, enum-ish reason the signin page can render.

    reasons: 'not_invited' | 'disabled' | 'email_unverified' | 'oauth_failed'
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class GoogleOAuth:
    """authlib Starlette client wrapper. Requires SessionMiddleware (the
    short-lived ``oauth_state`` cookie) for state/nonce round-tripping."""

    def __init__(self, client_id: str, client_secret: str):
        # imported lazily so `connect` stays importable without authlib in
        # exotic environments (workers never touch auth)
        from authlib.integrations.starlette_client import OAuth

        self._oauth = OAuth()
        self._oauth.register(
            "google",
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url=(
                "https://accounts.google.com/"
                ".well-known/openid-configuration"),
            client_kwargs={"scope": "openid email profile"},
        )

    async def authorize_redirect(self, request: Request,
                                 redirect_uri: str) -> Response:
        """302 to Google's consent screen (state/nonce in oauth_state)."""
        return await self._oauth.google.authorize_redirect(
            request, redirect_uri)

    async def exchange(self, request: Request) -> Mapping[str, Any]:
        """Callback half: code -> token -> VERIFIED ID-token claims.

        Raises LoginRejected('oauth_failed') on any authlib error (state
        mismatch, bad code, signature failure).
        """
        from authlib.integrations.base_client.errors import OAuthError

        try:
            token = await self._oauth.google.authorize_access_token(request)
        except OAuthError as e:
            raise LoginRejected(
                "oauth_failed", f"Google sign-in failed: {e.error}") from e
        claims = token.get("userinfo")
        if not claims:
            raise LoginRejected(
                "oauth_failed", "Google returned no ID token claims")
        return claims


async def login_identity(conn: psycopg.AsyncConnection, settings: Settings,
                         *, email: str, sub: str | None = None,
                         email_verified: bool = True,
                         name: str | None = None,
                         avatar_url: str | None = None,
                         invite_exempt: bool = False) -> CurrentUser:
    """The single login/provisioning gate (Google callback AND dev-login).

    invite_exempt=True is the dev hatch: the operator setting
    CONNECT_DEV_LOGIN_EMAIL *is* the authorization, so the invite gate is
    bypassed (first-user-admin and disabled checks still apply).
    """
    if not email or not email_verified:
        raise LoginRejected(
            "email_unverified",
            "Google did not supply a verified email for this account")
    email = email.lower()

    row = None
    if sub is not None:
        row = await user_dao.get_by_google_sub(conn, sub)
    if row is None:
        row = await user_dao.get_by_email(conn, email)
    if row is not None:
        if row["disabled"]:
            raise LoginRejected(
                "disabled", "This account has been disabled")
        row = await user_dao.record_login(
            conn, row["id"], google_sub=sub, name=name,
            avatar_url=avatar_url)
        return user_dao.to_model(row)

    # New identity — serialize creation so "first user ever" is exact.
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)",
                           (USER_CREATE_LOCK_KEY,))
        first_user = await user_dao.count(conn) == 0
        is_admin_email = email in {a.lower() for a in settings.admin_emails}
        allowed = (first_user or is_admin_email or invite_exempt
                   or settings.open_signup
                   or await invite_dao.get(conn, email) is not None)
        if not allowed:
            raise LoginRejected(
                "not_invited",
                "You're not on the invite list — ask the admin for an"
                " invite")
        role = "admin" if (first_user or is_admin_email) else "member"
        row = await user_dao.insert(
            conn, email=email, google_sub=sub, name=name,
            avatar_url=avatar_url, role=role)
    return user_dao.to_model(row)
