"""Instagram direct publishing via the Graph API (optional).

A deployment is "connected" when both Instagram credentials AND a public base
URL are configured (Instagram's servers fetch the rendered card from a public
URL, so localhost cannot publish). Publishing is the standard two-step image
flow: create a media container, then publish it.
"""

from __future__ import annotations

import asyncio
import time

import httpx

GRAPH = "https://graph.facebook.com/v21.0"

REEL_POLL_INTERVAL = 5.0     # seconds between container-status polls
REEL_POLL_TIMEOUT = 300.0    # give up if the reel never reaches FINISHED


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

        permalink = await _permalink(client, media_id, token)

    return {"media_id": media_id, "permalink": permalink}


async def publish_carousel(settings, *, image_urls: list[str],
                           caption: str) -> dict:
    """Create + publish a multi-image carousel: one child container per image
    (is_carousel_item), then a CAROUSEL parent, then publish. Returns
    {media_id, permalink}. Instagram allows 2-10 images."""
    token = settings.instagram_access_token
    ig_id = settings.instagram_business_account_id
    urls = [u for u in image_urls if u][:10]
    if len(urls) < 2:
        raise InstagramError("a carousel needs at least 2 images")
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            children: list[str] = []
            for url in urls:
                child = await client.post(
                    f"{GRAPH}/{ig_id}/media",
                    data={"image_url": url, "is_carousel_item": "true",
                          "access_token": token})
                child.raise_for_status()
                children.append(child.json()["id"])
            parent = await client.post(
                f"{GRAPH}/{ig_id}/media",
                data={"media_type": "CAROUSEL",
                      "children": ",".join(children),
                      "caption": caption, "access_token": token})
            parent.raise_for_status()
            creation_id = parent.json()["id"]
            pub = await client.post(
                f"{GRAPH}/{ig_id}/media_publish",
                data={"creation_id": creation_id, "access_token": token})
            pub.raise_for_status()
            media_id = pub.json()["id"]
        except httpx.HTTPStatusError as e:
            raise InstagramError(_graph_error(e)) from e
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise InstagramError(f"Instagram carousel failed: {e}") from e

        permalink = await _permalink(client, media_id, token)

    return {"media_id": media_id, "permalink": permalink}


async def publish_reel(settings, *, video_url: str, caption: str) -> dict:
    """Create + publish an Instagram Reel; returns {media_id, permalink}.

    Reels differ from images: the container is created with media_type=REELS +
    a public video_url, then Instagram transcodes ASYNCHRONOUSLY — we must poll
    the container's status_code until FINISHED before media_publish."""
    token = settings.instagram_access_token
    ig_id = settings.instagram_business_account_id
    timeout_s = getattr(settings, "instagram_reel_timeout_s", REEL_POLL_TIMEOUT)
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            create = await client.post(
                f"{GRAPH}/{ig_id}/media",
                data={"media_type": "REELS", "video_url": video_url,
                      "caption": caption, "access_token": token})
            create.raise_for_status()
            creation_id = create.json()["id"]

            deadline = time.monotonic() + timeout_s
            while True:
                st = await client.get(
                    f"{GRAPH}/{creation_id}",
                    params={"fields": "status_code", "access_token": token})
                st.raise_for_status()
                code = st.json().get("status_code")
                if code == "FINISHED":
                    break
                if code == "ERROR":
                    raise InstagramError(
                        "Instagram failed to process the reel video")
                if time.monotonic() > deadline:
                    raise InstagramError(
                        "Instagram reel processing timed out")
                await asyncio.sleep(REEL_POLL_INTERVAL)

            pub = await client.post(
                f"{GRAPH}/{ig_id}/media_publish",
                data={"creation_id": creation_id, "access_token": token})
            pub.raise_for_status()
            media_id = pub.json()["id"]
        except httpx.HTTPStatusError as e:
            raise InstagramError(_graph_error(e)) from e
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise InstagramError(f"Instagram reel publish failed: {e}") from e

        permalink = await _permalink(client, media_id, token)

    return {"media_id": media_id, "permalink": permalink}


async def _permalink(client: httpx.AsyncClient, media_id: str,
                     token: str) -> str | None:
    try:
        meta = await client.get(
            f"{GRAPH}/{media_id}",
            params={"fields": "permalink", "access_token": token})
        if meta.status_code == 200:
            return meta.json().get("permalink")
    except httpx.HTTPError:
        return None
    return None
