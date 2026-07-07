"""Background imagery for reels — fetch a relevant photo per scene from the
open internet so reels are visual, not text-on-a-color-block.

Source order: Pexels (if ``pexels_api_key`` is set — high-quality, portrait
orientation), then Openverse (keyless, CC-licensed aggregator), then
Wikimedia Commons photos (keyless; deep news/place coverage where the stock
aggregators are thin — an Indian courthouse, an effigy protest), broadening
the query word by word before giving up: under the scrim a slightly generic
photo beats the flat themed colour. Every step degrades gracefully to
``None`` so a missing/blocked image just falls back to the themed solid
background — image fetching never fails reel generation.

Pure-ish: network only. A small in-process cache avoids refetching the same
query across scenes/reels in a worker.
"""

from __future__ import annotations

import logging

import httpx

from connect.content import asset_cache

log = logging.getLogger(__name__)

# Wikimedia's API policy requires a UA that identifies the client AND gives
# a contact URL — anything less gets a non-JSON error page from Commons.
_UA = "connect-reel/0.2 (+https://example.org/connect; reel backgrounds)"
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


def _wikimedia_candidates(query: str) -> list[str]:
    """Keyless Commons PHOTO search — 1280px thumb URLs (served by Commons
    itself, so no hotlink blocks). Filenames must share a content word with
    the query; Commons' description matching is looser than a background
    photo can afford."""
    try:
        r = httpx.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "format": "json",
                    "generator": "search",
                    "gsrsearch": f"{query} filetype:bitmap",
                    "gsrnamespace": "6", "gsrlimit": "8",
                    "prop": "imageinfo", "iiprop": "url",
                    "iiurlwidth": "1280"},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT,
            follow_redirects=True)
        if r.status_code != 200:
            return []
        words = [w for w in query.lower().split() if len(w) > 3]
        out: list[str] = []
        pages = ((r.json().get("query") or {}).get("pages") or {})
        for p in pages.values():
            t = (p.get("title") or "").lower()
            if words and not any(w[:5] in t for w in words):
                continue
            for ii in p.get("imageinfo") or []:
                u = ii.get("thumburl") or ii.get("url")
                if u:
                    out.append(u)
        return out
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def _broaden(q: str) -> list[str]:
    """The query, then its generic cut (drop the leading word — news queries
    lead with the specific place: 'Narmadapuram courthouse India' ->
    'courthouse India'), then progressively shorter prefixes (a generic
    'courtroom' photo under the scrim beats the flat themed colour)."""
    words = q.split()
    out = [q]
    if len(words) >= 3:
        out.append(" ".join(words[1:]))
    out += [" ".join(words[:n]) for n in range(len(words) - 1, 0, -1)]
    seen: set[str] = set()
    return [v for v in out if v and not (v in seen or seen.add(v))]


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
    data = asset_cache.get(settings, "img", f"{q}|{variant}")
    if data is not None:
        _cache[ck] = data
        return data

    candidates: list[str] = []
    key = getattr(settings, "pexels_api_key", None)
    if key:
        # Pexels is professional stock: a specific news query often misses
        # while a broader cut of it hits great generic footage — try a few
        # (bounded: the free tier is 200 requests/hour)
        for v in _broaden(q)[:3]:
            candidates = _pexels_candidates(v, key)
            if candidates:
                break
    candidates += _openverse_candidates(q)
    if not candidates:
        # the stock aggregators are thin on news/place subjects — try
        # Commons photos, broadening the query before giving up entirely
        for v in _broaden(q):
            candidates = _wikimedia_candidates(v)
            if candidates:
                break
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
    else:
        asset_cache.put(settings, "img", f"{q}|{variant}", data)
    _cache[ck] = data
    return data
