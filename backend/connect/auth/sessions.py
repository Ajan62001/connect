"""App-session policy (tenancy design §3): opaque token, server-side row,
sliding expiry touched at most once per hour, 90-day hard cap from creation.

The cookie value is the session row's PRIMARY KEY (secrets.token_urlsafe(32)
— 256 bits, unguessable); nothing is signed because nothing is stored
client-side. Revocation is row deletion, period.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import psycopg

from connect.domain.models import CurrentUser
from connect.orchestration.config import Settings
from connect.storage import sessions as session_dao
from connect.storage import users as user_dao
from connect.storage.pg import utc_now

SESSION_COOKIE = "connect_session"


def _parse(ts: str) -> datetime:
    """utc_now()'s shape ('...mmmZ') back to an aware datetime."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")


def _fmt(dt: datetime) -> str:
    return (dt.astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")


def _expiry(created_at: str, settings: Settings) -> str:
    """Sliding window from now, hard-capped from the session's creation."""
    now = datetime.now(timezone.utc)
    sliding = now + timedelta(days=settings.session_ttl_days)
    cap = _parse(created_at) + timedelta(days=settings.session_max_days)
    return _fmt(min(sliding, cap))


async def create_session(conn: psycopg.AsyncConnection, user_id: int,
                         settings: Settings) -> str:
    """New session row; returns the opaque cookie value."""
    session_id = secrets.token_urlsafe(32)
    now = utc_now()
    await session_dao.insert(
        conn, session_id=session_id, user_id=user_id,
        expires_at=_expiry(now, settings))
    return session_id


async def resolve(conn: psycopg.AsyncConnection, session_id: str,
                  settings: Settings) -> CurrentUser | None:
    """Cookie value -> CurrentUser, or None (missing/expired row, disabled
    user). Touches the sliding expiry at most once per
    session_touch_seconds."""
    row = await session_dao.get_with_user(conn, session_id)
    if row is None:
        return None
    now = utc_now()
    if row["expires_at"] <= now:           # ISO strings compare lexically
        await session_dao.delete(conn, session_id)
        return None
    if row["disabled"]:
        return None
    last_seen = row["last_seen_at"]
    stale_after = _fmt(datetime.now(timezone.utc)
                       - timedelta(seconds=settings.session_touch_seconds))
    if last_seen is None or last_seen <= stale_after:
        await session_dao.touch(
            conn, session_id,
            _expiry(row["session_created_at"], settings))
    return user_dao.to_model(row)


async def destroy(conn: psycopg.AsyncConnection, session_id: str) -> bool:
    return await session_dao.delete(conn, session_id)
