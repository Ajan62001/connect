"""Stock VIDEO b-roll for reels — fetch a relevant short clip per scene so reels
move (real footage) instead of panning over a still.

Sources, in order:
  1. Pexels Videos (``pexels_api_key`` — same key as the photo path). Best
     quality/relevance, portrait clips. Skipped when no key.
  2. Wikimedia Commons (KEYLESS) — the MediaWiki API serving freely-licensed
     (CC / public-domain) video, legal to reuse. Lower relevance and often
     .webm/.ogv (ffmpeg transcodes these fine in the segment renderer). This is
     the "just works without a key" path.

We use official APIs, never HTML scraping. Every step degrades to ``None`` so a
miss just falls back to a photo Ken-Burns scene, then a themed colour — video
fetching NEVER fails reel generation. Network-only with a small in-process cache
(clips are big, so caching matters more than for images).
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

# Wikimedia's API blocks generic User-Agents — it requires one that identifies
# the client AND gives a contact URL, else it returns a non-JSON error page.
_UA = "connect-reel/0.2 (+https://example.org/connect; stock b-roll for reels)"
_MAX_BYTES = 60 * 1024 * 1024            # clips are large; cap the download
_TIMEOUT = 30.0
_TARGET_W = 720                          # prefer files near this width (speed)
_cache: dict[tuple[str, int], bytes | None] = {}


def _file_score(f: dict) -> tuple[int, int]:
    """Rank a Pexels video_file: portrait first, then width closest to target."""
    w = f.get("width") or 0
    h = f.get("height") or 0
    portrait = 0 if h >= w else 1
    return (portrait, abs((w or 99999) - _TARGET_W))


def _pexels_video_candidates(query: str, api_key: str) -> list[str]:
    """Best mp4 link per matching Pexels video (one file each, ranked)."""
    try:
        r = httpx.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": 5, "orientation": "portrait",
                    "size": "medium"},
            headers={"Authorization": api_key, "User-Agent": _UA},
            timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return []
        out: list[str] = []
        for v in r.json().get("videos") or []:
            files = [f for f in (v.get("video_files") or [])
                     if f.get("file_type") == "video/mp4" and f.get("link")]
            if not files:
                continue
            files.sort(key=_file_score)
            out.append(files[0]["link"])
        return out
    except (httpx.HTTPError, ValueError, KeyError):
        return []


_VIDEO_EXT = (".mp4", ".webm", ".mov", ".ogv", ".ogg", ".m4v")


def _wikimedia_video_candidates(query: str) -> list[str]:
    """Keyless CC/PD video file URLs from Wikimedia Commons, smallest first
    (so downloads stay cheap). Searches the File namespace for videos matching
    ``query`` and reads each file's direct URL + size via imageinfo."""
    try:
        r = httpx.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "format": "json",
                    "generator": "search",
                    "gsrsearch": f"{query} filetype:video",
                    "gsrnamespace": "6", "gsrlimit": "10",
                    "prop": "imageinfo", "iiprop": "url|mime|size"},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT,
            follow_redirects=True)
        if r.status_code != 200:
            return []
        pages = ((r.json().get("query") or {}).get("pages") or {})
        cands: list[tuple[int, str]] = []
        for p in pages.values():
            for ii in p.get("imageinfo") or []:
                url = ii.get("url")
                mime = ii.get("mime") or ""
                size = ii.get("size") or 0
                if not url or not (mime.startswith("video/") or "ogg" in mime):
                    continue
                if size and size > _MAX_BYTES:
                    continue
                cands.append((size or _MAX_BYTES, url))
        cands.sort(key=lambda x: x[0])
        return [u for _, u in cands]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def _query_variants(q: str) -> list[str]:
    """Progressively broader queries (full, then dropping trailing words) — the
    Commons video corpus is small, so a specific 3-4 word query often misses
    while its first 1-2 words hit. Tried in order until one returns results."""
    words = q.split()
    out, seen = [], set()
    for n in range(len(words), 0, -1):
        v = " ".join(words[:n])
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _download(url: str) -> bytes | None:
    try:
        with httpx.stream("GET", url, headers={"User-Agent": _UA},
                          timeout=_TIMEOUT, follow_redirects=True) as r:
            if r.status_code != 200:
                return None
            ctype = r.headers.get("content-type", "")
            path = url.split("?", 1)[0].lower()
            if not ("video" in ctype or "ogg" in ctype
                    or path.endswith(_VIDEO_EXT)):
                return None
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > _MAX_BYTES:
                    log.info("stock clip too large (>%dMB), skipping",
                             _MAX_BYTES // (1024 * 1024))
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError:
        return None


def available(settings) -> bool:
    """True — a stock-video source is always available (keyless Wikimedia
    Commons), with Pexels preferred when ``pexels_api_key`` is set."""
    return True


def fetch_scene_video(query: str, *, settings=None, variant: int = 0
                      ) -> bytes | None:
    """A relevant short stock clip for ``query`` as video bytes, or None.
    ``variant`` rotates which result is preferred so adjacent scenes differ.
    Cached by (query, variant). Tries Pexels (if key) then keyless Wikimedia
    Commons; returns None (caller falls back to a photo) on no result / download
    failure."""
    q = (query or "").strip()
    if not q:
        return None
    ck = (q, variant)
    if ck in _cache:
        return _cache[ck]

    candidates: list[str] = []
    key = getattr(settings, "pexels_api_key", None)
    if key:
        candidates += _pexels_video_candidates(q, key)
    if not candidates:                         # keyless fallback: broaden until hit
        for v in _query_variants(q):
            candidates = _wikimedia_video_candidates(v)
            if candidates:
                break
    if candidates and variant:
        candidates = candidates[variant % len(candidates):] \
            + candidates[:variant % len(candidates)]
    data = None
    for url in candidates:
        data = _download(url)
        if data is not None:
            break
    if data is None:
        log.info("no stock video for query %r", q)
    _cache[ck] = data
    return data
