"""Instagram direct publishing via the Graph API (optional).

A deployment is "connected" when both Instagram credentials AND a public base
URL are configured (Instagram's servers fetch the rendered card from a public
URL, so localhost cannot publish). Publishing is the standard two-step image
flow: create a media container, then publish it.
"""

from __future__ import annotations

import httpx

GRAPH = "https://graph.facebook.com/v21.0"


class InstagramError(Exception):
    """Graph API publish failure — surfaced to the caller as a 502."""


def is_configured(settings) -> bool:
    return bool(settings.instagram_access_token
               and settings.instagram_business_account_id
               and settings.public_base_url)


def _graph_error(exc: httpx.HTTPStatusError) -> str:
    try:
        err = exc.response.json().get("error", {})
        msg = err.get("message") or str(err)
    except ValueError:
        msg = exc.response.text[:300]
    return f"Instagram Graph API error: {msg}"


async def publish(settings, *, image_url: str, caption: str) -> dict:
    """Create + publish an image post; returns {media_id, permalink}."""
    token = settings.instagram_access_token
    ig_id = settings.instagram_business_account_id
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            create = await client.post(
                f"{GRAPH}/{ig_id}/media",
                data={"image_url": image_url, "caption": caption,
                      "access_token": token})
            create.raise_for_status()
            creation_id = create.json()["id"]
            pub = await client.post(
                f"{GRAPH}/{ig_id}/media_publish",
                data={"creation_id": creation_id, "access_token": token})
            pub.raise_for_status()
            media_id = pub.json()["id"]
        except httpx.HTTPStatusError as e:
            raise InstagramError(_graph_error(e)) from e
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise InstagramError(f"Instagram publish failed: {e}") from e

        permalink = None
        try:
            meta = await client.get(
                f"{GRAPH}/{media_id}",
                params={"fields": "permalink", "access_token": token})
            if meta.status_code == 200:
                permalink = meta.json().get("permalink")
        except httpx.HTTPError:
            permalink = None

    return {"media_id": media_id, "permalink": permalink}
