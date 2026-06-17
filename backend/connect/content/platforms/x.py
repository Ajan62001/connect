"""X (Twitter) thread publishing — SCAFFOLD.

The pipeline generates and queues X threads today; direct publishing waits on
API credentials. ``is_configured`` gates the live path; ``publish_thread``
chains tweets via reply (v2 POST /2/tweets, in_reply_to_tweet_id) once a
bearer token with write scope is set. Until then it raises NotConnected so the
content item lands in 'failed' with a clear, actionable error rather than a
silent drop.
"""

from __future__ import annotations

import httpx

from connect.content.platforms import NotConnected

API = "https://api.twitter.com/2"


def is_configured(settings) -> bool:
    return bool(getattr(settings, "x_api_bearer_token", None))


async def publish_thread(settings, *, tweets: list[str]) -> dict:
    """Post ``tweets`` as a reply-chain; returns {media_id, permalink} where
    media_id is the first tweet id. Raises NotConnected when unconfigured."""
    token = getattr(settings, "x_api_bearer_token", None)
    if not token:
        raise NotConnected(
            "X is not connected (set CONNECT_X_API_BEARER_TOKEN with write"
            " scope)")
    posts = [t for t in tweets if t.strip()]
    if not posts:
        raise NotConnected("nothing to post")
    headers = {"Authorization": f"Bearer {token}"}
    first_id: str | None = None
    reply_to: str | None = None
    async with httpx.AsyncClient(timeout=60.0) as client:
        for text in posts:
            body: dict = {"text": text[:280]}
            if reply_to:
                body["reply"] = {"in_reply_to_tweet_id": reply_to}
            resp = await client.post(f"{API}/tweets", json=body,
                                     headers=headers)
            resp.raise_for_status()
            tweet_id = resp.json()["data"]["id"]
            first_id = first_id or tweet_id
            reply_to = tweet_id
    permalink = f"https://x.com/i/web/status/{first_id}" if first_id else None
    return {"media_id": first_id or "", "permalink": permalink}
