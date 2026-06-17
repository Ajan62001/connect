"""Background imagery for reels — fetch a relevant photo per scene from the
open internet so reels are visual, not text-on-a-color-block.

Source order: Pexels (if ``pexels_api_key`` is set — high-quality, portrait
orientation), else Openverse (keyless, CC-licensed aggregator). Every step
degrades gracefully to ``None`` so a missing/blocked image just falls back to
the themed solid background — image fetching never fails reel generation.

Pure-ish: network only. A small in-process cache avoids refetching the same
query across scenes/reels in a worker.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

_UA = "connect/0.2 (+reel background imagery)"
_MAX_BYTES = 12 * 1024 * 1024
_TIMEOUT = 15.0
_cache: dict[str, bytes | None] = {}      # query -> image bytes (or None)


def _pexels_candidates(query: str, api_key: str) -> list[str]:
    try:
        r = httpx.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 3, "orientation": "portrait"},
            headers={"Authorization": api_key, "User-Agent": _UA},
            timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return []
        out = []
        for p in r.json().get("photos") or []:
            src = p.get("src") or {}
            u = src.get("portrait") or src.get("large2x") or src.get("large")
            if u:
                out.append(u)
        return out
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def _openverse_candidates(query: str) -> list[str]:
    # No aspect_ratio filter — it returns zero results for many queries; we
    # cover-crop anyway. Each result yields its direct url AND Openverse's
    # thumbnail proxy (which serves images whose origin hotlink-blocks us, e.g.
    # Wikimedia 403s the direct file but the proxy is fine).
    try:
        r = httpx.get(
            "https://api.openverse.org/v1/images/",
            params={"q": query, "page_size": 5, "license_type": "all"},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT,
            follow_redirects=True)
        if r.status_code != 200:
            return []
        urls, thumbs = [], []
        for it in r.json().get("results", []):
            if it.get("url"):
                urls.append(it["url"])
            if it.get("thumbnail"):
                thumbs.append(it["thumbnail"])
        # direct URLs first (distinct images, so variant-rotation varies the
        # photo), thumbnails after as a reliable fallback for hotlink-blocked ones
        return urls + thumbs
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def _download(url: str) -> bytes | None:
    try:
        with httpx.stream("GET", url, headers={"User-Agent": _UA},
                          timeout=_TIMEOUT, follow_redirects=True) as r:
            if r.status_code != 200:
                return None
            ctype = r.headers.get("content-type", "")
            if "image" not in ctype:
                return None
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > _MAX_BYTES:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError:
        return None


def fetch_scene_image(query: str, *, settings=None, variant: int = 0
                      ) -> bytes | None:
    """A relevant photo for ``query`` as image bytes, or None. ``variant`` picks
    a different result for the same query so adjacent scenes don't all show the
    identical top hit. Cached by (query, variant). Tries Pexels (if key) then
    Openverse, downloading candidates until one yields a real image."""
    q = (query or "").strip()
    if not q:
        return None
    ck = (q, variant)
    if ck in _cache:
        return _cache[ck]

    candidates: list[str] = []
    key = getattr(settings, "pexels_api_key", None)
    if key:
        candidates += _pexels_candidates(q, key)
    candidates += _openverse_candidates(q)
    # rotate the candidate order by variant so scene N prefers the Nth result
    if candidates and variant:
        variant %= len(candidates)
        candidates = candidates[variant:] + candidates[:variant]

    data = None
    for url in candidates:
        data = _download(url)
        if data is not None:
            break
    if data is None:
        log.info("no background image for query %r", q)
    _cache[ck] = data
    return data
