"""Stock VIDEO b-roll for reels — fetch a relevant short clip per scene so reels
move (real footage) instead of panning over a still.

Sources, in order:
  1. Pexels Videos (``pexels_api_key`` — same key as the photo path). Best
     quality/relevance, portrait clips. Skipped when no key.
  2. Wikimedia Commons (KEYLESS) — the MediaWiki API serving freely-licensed
     (CC / public-domain) video, legal to reuse. Lower relevance and often
     .webm/.ogv (ffmpeg transcodes these fine in the segment renderer). This is
     the "just works without a key" path.
  3. Internet Archive (KEYLESS) — public-domain / CC newsreel and stock
     collections, merged with the Commons results so keyless coverage isn't
     limited to what Commons happens to hold.

We use official APIs, never HTML scraping. Every step degrades to ``None`` so a
miss just falls back to a photo Ken-Burns scene, then a themed colour — video
fetching NEVER fails reel generation. Network-only with a small in-process cache
(clips are big, so caching matters more than for images).
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx

from connect.content import asset_cache

log = logging.getLogger(__name__)

# Wikimedia's API blocks generic User-Agents — it requires one that identifies
# the client AND gives a contact URL, else it returns a non-JSON error page.
_UA = "connect-reel/0.2 (+https://example.org/connect; stock b-roll for reels)"
_MAX_BYTES = 60 * 1024 * 1024            # clips are large; cap the download
_TIMEOUT = 30.0
_TARGET_W = 1080                         # the output frame width — prefer
                                         # sources at/near it (upscaling small
                                         # clips is what makes reels look soft)
_MIN_SOURCE_W = 480                      # below this the upscale is mush
_cache: dict[tuple[str, int, int], bytes | None] = {}


def _file_score(f: dict) -> tuple[int, int]:
    """Rank a Pexels video_file: portrait first, then width closest to the
    output frame (never knowingly upscale when a bigger file exists)."""
    w = f.get("width") or 0
    h = f.get("height") or 0
    portrait = 0 if h >= w else 1
    return (portrait, abs((w or 99999) - _TARGET_W))


def _pexels_video_candidates(query: str, api_key: str,
                             min_seconds: float = 0.0) -> list[str]:
    """Best mp4 link per matching Pexels video, videos that COVER the scene
    duration first (a too-short clip has to loop, and the loop restart reads
    as a jump cut mid-scene)."""
    try:
        r = httpx.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": 5, "orientation": "portrait",
                    "size": "medium"},
            headers={"Authorization": api_key, "User-Agent": _UA},
            timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return []
        ranked: list[tuple[int, int, str]] = []
        for i, v in enumerate(r.json().get("videos") or []):
            files = [f for f in (v.get("video_files") or [])
                     if f.get("file_type") == "video/mp4" and f.get("link")]
            if not files:
                continue
            files.sort(key=_file_score)
            covers = 0 if float(v.get("duration") or 0) >= min_seconds else 1
            ranked.append((covers, i, files[0]["link"]))
        ranked.sort()
        return [(c, u) for c, _, u in ranked]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


_VIDEO_EXT = (".mp4", ".webm", ".mov", ".ogv", ".ogg", ".m4v")


# NOTE: place names are deliberately NOT stopwords — 'Indian district court'
# must not match every US '...District Court...' nomination upload; the
# geography is the strongest disambiguator these short queries carry.
_STOPWORDS = frozenset(
    "the a an of in on at to for and or with over under near".split())


def _content_words(q: str) -> list[str]:
    return [w for w in (q or "").lower().split()
            if len(w) > 3 and w not in _STOPWORDS]


def _title_matches(title: str, query: str) -> bool:
    """Commons full-text search matches file DESCRIPTIONS too, which lands
    completely unrelated clips (vlogs, agency slates) behind news scenes.
    PRECISION over recall: a clip qualifies only when its FILENAME shares
    (almost) the whole query — up to three content words, prefix-matched
    (filenames concatenate words). Two shared words are NOT enough: 'Indian
    district court building' word-matches every US '...District Court...'
    hearing upload. A rejected clip just means the scene uses the (reliably
    on-subject) photo path instead; with no usable query words, let it
    through."""
    words = _content_words(query)
    if not words:
        return True
    t = (title or "").lower()
    hits = sum(1 for w in words if w[:5] in t)
    return hits >= min(3, len(words))


def _wikimedia_video_candidates(query: str,
                                min_seconds: float = 0.0) -> list[str]:
    """Keyless CC/PD video file URLs from Wikimedia Commons. Searches the
    File namespace for videos matching ``query`` and reads each file's direct
    URL + dimensions/duration via imageinfo. Files whose NAME shares nothing
    with the query are dropped (an irrelevant clip is worse than the photo
    fallback), as are tiny sources (the upscale to 1080x1920 is what makes
    reels look soft). Rank: clips that COVER the scene without looping first
    (a loop restart reads as a jump cut), then resolution closest to the
    output frame — never smallest-file-first."""
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
        cands: list[tuple[int, int, int, str]] = []
        for p in pages.values():
            if not _title_matches(p.get("title") or "", query):
                continue
            for ii in p.get("imageinfo") or []:
                url = ii.get("url")
                mime = ii.get("mime") or ""
                size = ii.get("size") or 0
                width = int(ii.get("width") or 0)
                dur = float(ii.get("duration") or 0.0)
                if not url or not (mime.startswith("video/") or "ogg" in mime):
                    continue
                if size and size > _MAX_BYTES:
                    continue
                if width and width < _MIN_SOURCE_W:
                    continue
                covers = 0 if dur and dur >= min_seconds else 1
                cands.append((covers, abs((width or 99999) - _TARGET_W),
                              size or _MAX_BYTES, url))
        cands.sort()
        return [(c, u) for c, _, _, u in cands]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def _archive_duration(value: object) -> float:
    """archive.org file 'length' — seconds ('123.45') or 'H:MM:SS'."""
    s = str(value or "").strip()
    if not s:
        return 0.0
    try:
        if ":" in s:
            parts = [float(p) for p in s.split(":")]
            out = 0.0
            for p in parts:
                out = out * 60 + p
            return out
        return float(s)
    except ValueError:
        return 0.0


def _archive_video_candidates(query: str,
                              min_seconds: float = 0.0) -> list[str]:
    """Keyless Internet Archive footage — a second free video source so
    scenes aren't limited to what Wikimedia Commons happens to hold.
    Restricted to items carrying an explicit license URL (CC / PD marks) or
    living in the classic public-domain film collections — the Archive also
    mirrors plenty of YouTube uploads of unclear provenance, and an
    unlicensed clip in a published reel is a copyright strike. Matching
    items are title-relevance filtered like Commons, then each item's file
    listing yields its best mp4 derivative: covers-the-scene first, then
    resolution closest to the output frame."""
    try:
        r = httpx.get(
            "https://archive.org/advancedsearch.php",
            params={"q": f"({query}) AND mediatype:movies"
                         " AND (licenseurl:* OR collection:(prelinger"
                         " OR newsandpublicaffairs OR opensource_movies))",
                    "fl[]": ["identifier", "title"],
                    "rows": "6", "page": "1", "output": "json"},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT,
            follow_redirects=True)
        if r.status_code != 200:
            return []
        # cap the metadata fan-out: each doc costs one more GET, and this
        # runs inline in the render path
        docs = ((r.json().get("response") or {}).get("docs") or [])[:4]
    except (httpx.HTTPError, ValueError, KeyError):
        return []
    out: list[tuple[int, int, str]] = []
    for d in docs:
        ident = d.get("identifier")
        title = d.get("title")
        title = title[0] if isinstance(title, list) else (title or "")
        if not ident or not _title_matches(str(title), query):
            continue
        # YouTube mirrors carry uploader-set license claims that can't be
        # trusted — a struck reel is worse than a plain photo scene.
        if "youtube" in ident.lower() or "yt-" in ident.lower():
            continue
        try:
            m = httpx.get(f"https://archive.org/metadata/{ident}",
                          headers={"User-Agent": _UA}, timeout=_TIMEOUT,
                          follow_redirects=True)
            if m.status_code != 200:
                continue
            files = m.json().get("files") or []
        except (httpx.HTTPError, ValueError, KeyError):
            continue
        best: tuple[int, int, str] | None = None
        for f in files:
            name = str(f.get("name") or "")
            if not name.lower().endswith(".mp4"):
                continue
            try:
                size = int(f.get("size") or 0)
                width = int(f.get("width") or 0)
            except (TypeError, ValueError):
                continue
            if not size or size > _MAX_BYTES:
                continue
            if width and width < _MIN_SOURCE_W:
                continue
            dur = _archive_duration(f.get("length"))
            covers = 0 if dur and dur >= min_seconds else 1
            url = (f"https://archive.org/download/{ident}/"
                   f"{urllib.parse.quote(name)}")
            cand = (covers, abs((width or 99999) - _TARGET_W), url)
            if best is None or cand[:2] < best[:2]:
                best = cand
        if best is not None:
            out.append(best)
    out.sort(key=lambda c: c[:2])
    return [(c, u) for c, _, u in out]


def _query_variants(q: str) -> list[str]:
    """Progressively broader queries (full, then dropping trailing words) —
    the free video corpora are small, so a specific 3-4 word query often
    misses while a shorter prefix hits. BUT broadening stops while the
    variant still carries at least two content words (when the query has
    them): degrading to one generic word ('protest', 'court') reliably lands
    a clip about the wrong story, and a moving-but-wrong clip is worse than
    the on-subject photo the caller falls back to."""
    words = q.split()
    floor = min(2, len(_content_words(q))) or 1
    raw = [q]
    if len(words) >= 3:
        # the generic cut: news queries lead with the specific place/name
        # ('Narmadapuram courthouse India' -> 'courthouse India')
        raw.append(" ".join(words[1:]))
    raw += [" ".join(words[:n]) for n in range(len(words) - 1, 0, -1)]
    out, seen = [], set()
    for v in raw:
        if v and v not in seen and len(_content_words(v)) >= floor:
            seen.add(v)
            out.append(v)
    return out or [q]


def _rotate_grouped(candidates: list[tuple[int, str]],
                    variant: int) -> list[str]:
    """Flatten (covers, url) candidates to urls, rotating by ``variant``
    INSIDE each covers-group: adjacent scenes still get different clips, but
    rotation never promotes a too-short (looping) clip over one that plays
    the scene through."""
    if not candidates:
        return []
    groups: dict[int, list[str]] = {}
    for covers, url in candidates:
        groups.setdefault(covers, []).append(url)
    out: list[str] = []
    for covers in sorted(groups):
        g = groups[covers]
        k = variant % len(g)
        out.extend(g[k:] + g[:k])
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


def fetch_scene_video(query: str, *, settings=None, variant: int = 0,
                      min_seconds: float = 0.0) -> bytes | None:
    """A relevant short stock clip for ``query`` as video bytes, or None.
    ``variant`` rotates which result is preferred so adjacent scenes differ;
    ``min_seconds`` is the scene duration the clip should cover (shorter
    clips loop with a visible jump cut, so covering clips rank first).
    Cached by (query, variant, ceil(min_seconds)). Tries Pexels (if key)
    then keyless Wikimedia Commons; returns None (caller falls back to a
    photo) on no result / download failure."""
    q = (query or "").strip()
    if not q:
        return None
    bucket = int(min_seconds + 0.999)
    ck = (q, variant, bucket)
    if ck in _cache:
        return _cache[ck]
    data = asset_cache.get(settings, "vid", f"{q}|{variant}|{bucket}")
    if data is not None:
        _cache[ck] = data
        return data

    candidates: list[tuple[int, str]] = []
    key = getattr(settings, "pexels_api_key", None)
    if key:
        # a specific news query often misses Pexels while a broader cut hits
        # great professional footage — try a few (bounded: 200 req/hour tier)
        for v in _query_variants(q)[:3]:
            candidates = _pexels_video_candidates(v, key, min_seconds)
            if candidates:
                break
    if not candidates:                         # keyless fallback: broaden until hit
        for v in _query_variants(q):
            # Archive only when Commons misses — its per-item metadata GETs
            # are the slowest step of the render's fetch phase
            candidates = _wikimedia_video_candidates(v, min_seconds) \
                or _archive_video_candidates(v, min_seconds)
            if candidates:
                break
    # rotate WITHIN each covers-group so adjacent scenes still differ but a
    # scene never trades a play-through clip for a looping one
    urls = _rotate_grouped(candidates, variant)
    data = None
    for url in urls:
        data = _download(url)
        if data is not None:
            break
    if data is None:
        log.info("no stock video for query %r", q)
    else:
        asset_cache.put(settings, "vid", f"{q}|{variant}|{bucket}", data)
    _cache[ck] = data
    return data
