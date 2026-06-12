"""The /api/auth surface + /api/me (tenancy design §3, Phase B).

Flow: GET /auth/login -> Google consent -> GET /auth/callback (verify ID
token, gate, create session row, set the opaque httpOnly cookie) -> 302
back to the frontend. POST /auth/logout deletes the row. GET /auth/methods
tells the signin page which paths exist. POST /auth/dev-login is the
env-gated, dev-only escape hatch (404 unless CONNECT_DEV_LOGIN_EMAIL is
set) so the system stays usable before Google credentials exist.

These routes (and /api/health) are the ONLY unauthenticated ones; rejected
logins redirect to /signin?error=<reason> — a friendly signed-out page, no
user row created.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from connect.api.deps import get_container, get_current_user, get_db
from connect.auth import sessions as auth_sessions
from connect.auth.oauth import LoginRejected, login_identity
from connect.auth.sessions import SESSION_COOKIE
from connect.domain.models import AuthMethods, CurrentUser
from connect.orchestration.container import Container

router = APIRouter(tags=["auth"])


def _set_session_cookie(response: Response, request: Request,
                        session_id: str, max_age_days: int) -> None:
    response.set_cookie(
        SESSION_COOKIE, session_id,
        max_age=max_age_days * 86400,
        httponly=True,
        samesite="lax",
        # uvicorn --proxy-headers makes url.scheme honest behind Caddy/TLS
        secure=request.url.scheme == "https",
        path="/",
    )


def _signin_redirect(container: Container, reason: str) -> RedirectResponse:
    base = container.settings.frontend_origin.rstrip("/")
    return RedirectResponse(f"{base}/signin?error={reason}", status_code=302)


@router.get("/auth/methods", response_model=AuthMethods)
async def auth_methods(container: Container = Depends(get_container)):
    """Which sign-in paths this deployment offers (drives the signin page)."""
    return AuthMethods(
        google=container.google_oauth is not None,
        dev=bool(container.settings.dev_login_email),
    )


@router.get("/auth/login")
async def auth_login(request: Request,
                     container: Container = Depends(get_container)):
    """302 to Google's consent screen (state/nonce in the short-lived
    itsdangerous-signed oauth_state cookie — SessionMiddleware)."""
    oauth = container.google_oauth
    if oauth is None:
        raise HTTPException(
            status_code=404,
            detail="Google sign-in is not configured"
                   " (GOOGLE_OAUTH_CLIENT_ID/SECRET unset)")
    redirect_uri = (container.settings.oauth_redirect_uri
                    or str(request.url_for("auth_callback")))
    return await oauth.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request,
                        container: Container = Depends(get_container),
                        db: psycopg.AsyncConnection = Depends(get_db)):
    """Code -> verified ID-token claims -> gate -> session cookie -> app."""
    oauth = container.google_oauth
    if oauth is None:
        raise HTTPException(status_code=404,
                            detail="Google sign-in is not configured")
    try:
        claims = await oauth.exchange(request)
        user = await login_identity(
            db, container.settings,
            email=claims.get("email") or "",
            sub=claims.get("sub"),
            email_verified=bool(claims.get("email_verified")),
            name=claims.get("name"),
            avatar_url=claims.get("picture"),
        )
    except LoginRejected as e:
        return _signin_redirect(container, e.reason)
    session_id = await auth_sessions.create_session(
        db, user.id, container.settings)
    base = container.settings.frontend_origin.rstrip("/")
    response = RedirectResponse(f"{base}/", status_code=302)
    _set_session_cookie(response, request, session_id,
                        container.settings.session_ttl_days)
    return response


@router.post("/auth/dev-login", response_model=CurrentUser)
async def dev_login(request: Request, response: Response,
                    container: Container = Depends(get_container),
                    db: psycopg.AsyncConnection = Depends(get_db)):
    """DEV-ONLY escape hatch: sign in as exactly CONNECT_DEV_LOGIN_EMAIL.

    Refuses to exist (404) when the env is unset; never reads an email from
    the request, so it cannot register arbitrary users. Setting the env IS
    the authorization (invite gate bypassed); first-user-becomes-admin and
    the disabled check still apply. Excluded from production by never
    setting the variable there.
    """
    email = container.settings.dev_login_email
    if not email:
        raise HTTPException(status_code=404, detail="dev login is disabled")
    try:
        user = await login_identity(
            db, container.settings, email=email,
            name="Dev User", invite_exempt=True)
    except LoginRejected as e:
        raise HTTPException(status_code=403, detail=e.detail)
    session_id = await auth_sessions.create_session(
        db, user.id, container.settings)
    _set_session_cookie(response, request, session_id,
                        container.settings.session_ttl_days)
    return user


@router.post("/auth/logout", status_code=204, response_class=Response)
async def logout(request: Request,
                 db: psycopg.AsyncConnection = Depends(get_db)):
    """Delete the session row + clear the cookie. Deliberately tolerant:
    works (and clears) even when the session is already gone/expired."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        await auth_sessions.destroy(db, session_id)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=CurrentUser)
async def me(user: CurrentUser = Depends(get_current_user)):
    """The authenticated requester (id/email/name/avatar/role + login
    stamps). Budgets + today's spend join this payload in Phase D."""
    return user
