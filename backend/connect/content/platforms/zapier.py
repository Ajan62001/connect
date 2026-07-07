"""Zapier webhook publishing — route a content item to a Zapier "Catch Hook".

When ``CONNECT_ZAPIER_WEBHOOK_URL`` is set, the review queue offers "Send to
Zapier" per draft: publishing POSTs a JSON payload (format, platform, caption,
hashtags, public media URLs) to the webhook, and the user's Zap does the actual
posting — Instagram, X, LinkedIn, Facebook, Buffer, a spreadsheet, whatever.
This sidesteps per-platform API credentials and hands downstream control to the
user. Raises ``NotConnected`` when no webhook is configured.
"""

from __future__ import annotations

from typing import Any

import httpx

from connect.content.platforms import NotConnected


def is_configured(settings) -> bool:
    return bool(getattr(settings, "zapier_webhook_url", None))


async def publish(settings, *, payload: dict[str, Any],
                  url_override: str | None = None) -> dict:
    """POST ``payload`` to a Zapier Catch Hook. ``url_override`` (a workspace
    channel's own webhook) wins over the global ``CONNECT_ZAPIER_WEBHOOK_URL``,
    so each channel can fan out to its own Zap. Returns a publish ref
    ({media_id, permalink, via}); raises NotConnected when neither is set."""
    url = url_override or getattr(settings, "zapier_webhook_url", None)
    if not url:
        raise NotConnected(
            "Zapier is not connected (set CONNECT_ZAPIER_WEBHOOK_URL to a"
            " Zapier 'Catch Hook' URL, or a per-workspace channel webhook)")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
    # Zapier hooks return {"status": "success", "request_id": "..."}.
    return {"media_id": str(data.get("request_id") or ""),
            "permalink": None, "via": "zapier"}
