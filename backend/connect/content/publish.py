"""Publish dispatch — route a content item to its platform adapter.

The handler/beat path calls ``publish_item``; the API status endpoint calls
``connected_platforms``. Captions/hashtags are assembled here so the adapters
stay transport-only. Raises the platform's error (InstagramError /
NotConnected) on failure — the caller marks the item 'failed'.
"""

from __future__ import annotations

from typing import Any

from connect.content.platforms import linkedin, x, zapier
from connect.content.schema import (
    CarouselContent,
    LinkedInContent,
    MemeContent,
    ReelContent,
    ThreadContent,
)
from connect.domain.enums import CONTENT_FORMAT_PLATFORM
from connect.domain.models import SocialPost
from connect.social import instagram


def _tags(hashtags: list[str]) -> str:
    return " ".join(f"#{t.lstrip('#')}" for t in hashtags if t.strip())


def _with_tags(text: str, hashtags: list[str]) -> str:
    tags = _tags(hashtags)
    return f"{text}\n\n{tags}".strip() if tags else text


def connected_platforms(settings) -> dict[str, bool]:
    """Which platforms can publish directly right now."""
    return {
        "instagram": instagram.is_configured(settings),
        "x": x.is_configured(settings),
        "linkedin": linkedin.is_configured(settings),
    }


def zapier_configured(settings) -> bool:
    """Whether the 'Send to Zapier' publish route is available."""
    return zapier.is_configured(settings)


def _shape(fmt: str, content: dict[str, Any]) -> tuple[str, str, list[str], str]:
    """Per-format (caption_with_tags, plain_text, hashtags, media_type)."""
    if fmt == "ig_card":
        p = SocialPost(**content)
        return _with_tags(p.caption, p.hashtags), p.caption, list(p.hashtags), "image"
    if fmt == "ig_carousel":
        c = CarouselContent(**content)
        return _with_tags(c.caption, c.hashtags), c.caption, list(c.hashtags), "image"
    if fmt == "ig_reel":
        r = ReelContent(**content)
        return _with_tags(r.caption, r.hashtags), r.caption, list(r.hashtags), "video"
    if fmt == "x_thread":
        t = ThreadContent(**content)
        text = "\n\n".join(t.tweets)
        return _with_tags(text, t.hashtags), text, list(t.hashtags), "none"
    if fmt == "linkedin_post":
        li = LinkedInContent(**content)
        return _with_tags(li.body, li.hashtags), li.body, list(li.hashtags), "none"
    if fmt == "meme":
        m = MemeContent(**content)
        return _with_tags(m.caption, m.hashtags), m.caption, list(m.hashtags), "image"
    raise ValueError(f"unknown format {fmt!r}")


def build_zapier_payload(fmt: str, content: dict[str, Any],
                         card_urls: list[str], *, item_id: int) -> dict:
    """The JSON a Zap receives — everything needed to post anywhere."""
    caption, text, hashtags, media_type = _shape(fmt, content)
    return {
        "item_id": item_id,
        "format": fmt,
        "platform": CONTENT_FORMAT_PLATFORM.get(fmt, ""),
        "caption": caption,
        "text": text,
        "hashtags": hashtags,
        "media_urls": list(card_urls),
        "media_type": media_type if card_urls else "none",
    }


async def publish_via_zapier(settings, *, fmt: str, content: dict[str, Any],
                             card_urls: list[str], item_id: int) -> dict:
    """Send one item to the configured Zapier webhook (the user's Zap posts it)."""
    payload = build_zapier_payload(fmt, content, card_urls, item_id=item_id)
    return await zapier.publish(settings, payload=payload)


async def publish_item(settings, *, fmt: str, content: dict[str, Any],
                       card_urls: list[str]) -> dict:
    """Publish one item; returns the adapter's {media_id, permalink}."""
    if fmt == "ig_card":
        post = SocialPost(**content)
        if not card_urls:
            raise instagram.InstagramError("no rendered card to publish")
        return await instagram.publish(
            settings, image_url=card_urls[0],
            caption=_with_tags(post.caption, post.hashtags))
    if fmt == "ig_carousel":
        car = CarouselContent(**content)
        return await instagram.publish_carousel(
            settings, image_urls=card_urls,
            caption=_with_tags(car.caption, car.hashtags))
    if fmt == "ig_reel":
        reel = ReelContent(**content)
        if not card_urls:
            raise instagram.InstagramError("no rendered reel to publish")
        return await instagram.publish_reel(
            settings, video_url=card_urls[0],
            caption=_with_tags(reel.caption, reel.hashtags))
    if fmt == "x_thread":
        thread = ThreadContent(**content)
        tweets = list(thread.tweets)
        if tweets and thread.hashtags:
            tweets[-1] = _with_tags(tweets[-1], thread.hashtags)[:280]
        return await x.publish_thread(settings, tweets=tweets)
    if fmt == "linkedin_post":
        li = LinkedInContent(**content)
        return await linkedin.publish(
            settings, body=_with_tags(li.body, li.hashtags))
    if fmt == "meme":
        m = MemeContent(**content)
        if not card_urls:
            raise instagram.InstagramError("no rendered meme to publish")
        return await instagram.publish(
            settings, image_url=card_urls[0],
            caption=_with_tags(m.caption, m.hashtags))
    raise ValueError(f"unknown format {fmt!r}")
