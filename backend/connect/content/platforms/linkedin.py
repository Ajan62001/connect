"""LinkedIn post publishing — SCAFFOLD.

Generation + queue + scheduling work today; direct publishing waits on an
access token + author URN. ``publish`` shapes the UGC Posts call (POST
/v2/ugcPosts as a TEXT share) once credentials are set, and otherwise raises
NotConnected so the item fails loudly with an actionable message.
"""

from __future__ import annotations

import httpx

from connect.content.platforms import NotConnected

API = "https://api.linkedin.com/v2/ugcPosts"


def is_configured(settings) -> bool:
    return bool(getattr(settings, "linkedin_access_token", None)
                and getattr(settings, "linkedin_author_urn", None))


async def publish(settings, *, body: str) -> dict:
    token = getattr(settings, "linkedin_access_token", None)
    urn = getattr(settings, "linkedin_author_urn", None)
    if not (token and urn):
        raise NotConnected(
            "LinkedIn is not connected (set CONNECT_LINKEDIN_ACCESS_TOKEN and"
            " CONNECT_LINKEDIN_AUTHOR_URN)")
    payload = {
        "author": urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": body},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    headers = {"Authorization": f"Bearer {token}",
               "X-Restli-Protocol-Version": "2.0.0"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(API, json=payload, headers=headers)
        resp.raise_for_status()
        post_id = resp.json().get("id", "")
    return {"media_id": post_id, "permalink": None}
