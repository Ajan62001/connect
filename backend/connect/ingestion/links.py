"""In-content link extraction & classification — pure functions, no DB.

Source material is the stored raw HTML blob (trafilatura's text output has
no anchors); parsed with lxml.html (a hard transitive dependency of
trafilatura — verified in this venv).

Keep rule — a link survives classification iff ANY of:
  (a) its anchor text (normalized, len >= 3) appears in the extracted
      content_text — i.e. it is a CONTENT link, not nav/boilerplate;
  (b) its path looks like a document file (.pdf/.doc/.docx/.xls/.xlsx/...);
  (c) its domain is on the official allowlist AND differs from the parent's
      registrable domain. The cross-domain restriction matters: on an
      official site (rbi.org.in, pib.gov.in) EVERY nav link is allowlisted,
      so a flat rule (c) would keep the whole site chrome. Same-site links
      must earn their keep via (a) or (b) — the RBI circular's PDF twin
      comes in through (b).

``is_official`` stays the pure column meaning (target domain on the
allowlist) regardless of WHICH rule kept the link.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

log = logging.getLogger(__name__)

MAX_LINKS_PER_DOC = 50
_MAX_ANCHOR_CHARS = 300
_MIN_ANCHOR_CHARS = 3

FILE_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")

_TRACKING_PARAMS = frozenset({
    "gclid", "fbclid", "igshid", "msclkid", "yclid",
    "mc_cid", "mc_eid", "_ga", "ref_src", "cmpid"})

# Two-label public suffixes we actually meet (Indian registry + common
# generics) — enough for registrable-domain grouping without a PSL dep.
_TWO_LABEL_SUFFIXES = frozenset({
    "gov.in", "nic.in", "ac.in", "co.in", "org.in", "net.in", "res.in",
    "edu.in", "mil.in", "gen.in", "firm.in", "ind.in",
    "co.uk", "org.uk", "gov.uk", "ac.uk", "com.au", "gov.au"})


@dataclass(frozen=True)
class ClassifiedLink:
    url: str
    anchor_text: str | None
    is_file: bool
    is_official: bool


# --- URL helpers --------------------------------------------------------------

def clean_url(url: str) -> str:
    """Strip the fragment and utm_*/tracking query params."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")
             and k.lower() not in _TRACKING_PARAMS]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def registrable_domain(host: str) -> str:
    """eTLD+1-ish grouping: 'www.rbi.org.in' -> 'rbi.org.in',
    'pib.gov.in' -> 'pib.gov.in', 'm.thehindu.com' -> 'thehindu.com'."""
    host = host.lower().strip(".")
    labels = host.split(".")
    if len(labels) < 2:
        return host
    if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_official_domain(host: str, official_domains: Sequence[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in official_domains)


def is_file_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(FILE_EXTENSIONS)


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _anchor_in_content(anchor_norm: str, content_norm: str) -> bool:
    """Word-boundary containment, not bare substring — short nav anchors
    ('Act', 'Go') otherwise match inside words ('exact', 'category');
    verified against the real RBI corpus."""
    pattern = r"(?<!\w)" + re.escape(anchor_norm) + r"(?!\w)"
    return re.search(pattern, content_norm) is not None


# --- extraction ----------------------------------------------------------------

def extract_anchors(raw: bytes | str,
                    base_url: str | None) -> list[tuple[str, str | None]]:
    """All <a href> in the page -> ordered, deduped (url, anchor_text) pairs.

    Relative URLs resolve against ``base_url`` (dropped when there is none —
    e.g. an uploaded HTML file); only http(s) survives; fragments and
    tracking params are stripped; mailto/javascript/same-page are dropped.
    """
    from lxml import html as lxml_html  # lazy heavy import

    try:
        root = lxml_html.fromstring(raw)
    except Exception:  # noqa: BLE001 — unparseable HTML yields no links
        log.debug("lxml could not parse HTML for link extraction")
        return []

    seen: dict[str, str | None] = {}
    for a in root.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        lowered = href.lower()
        if lowered.startswith(("mailto:", "javascript:", "tel:", "data:")):
            continue
        url = urljoin(base_url, href) if base_url else href
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        url = clean_url(url)
        anchor = " ".join(a.text_content().split())[:_MAX_ANCHOR_CHARS] or None
        if url not in seen:
            seen[url] = anchor
        elif seen[url] is None and anchor:
            seen[url] = anchor  # first occurrence wins, but backfill empty text
    return list(seen.items())


# --- classification --------------------------------------------------------------

def classify_links(pairs: Iterable[tuple[str, str | None]], *,
                   content_text: str,
                   self_urls: Sequence[str | None],
                   official_domains: Sequence[str],
                   max_links: int = MAX_LINKS_PER_DOC) -> list[ClassifiedLink]:
    """Apply the keep rule (module docstring) and the per-document cap."""
    content_norm = _normalize_text(content_text)
    own = {clean_url(u) for u in self_urls if u}
    own_domains = {registrable_domain(_host(u)) for u in own}

    kept: list[ClassifiedLink] = []
    for url, anchor in pairs:
        if len(kept) >= max_links:
            break
        if url in own:
            continue
        host = _host(url)
        file_link = is_file_url(url)
        official = is_official_domain(host, official_domains)
        cross_domain = registrable_domain(host) not in own_domains
        anchor_norm = _normalize_text(anchor) if anchor else ""
        in_content = (len(anchor_norm) >= _MIN_ANCHOR_CHARS
                      and _anchor_in_content(anchor_norm, content_norm))
        if in_content or file_link or (official and cross_domain):
            kept.append(ClassifiedLink(
                url=url, anchor_text=anchor,
                is_file=file_link, is_official=official))
    return kept


def classify_urls(urls: Iterable[str], *, self_urls: Sequence[str | None],
                  official_domains: Sequence[str],
                  max_links: int = MAX_LINKS_PER_DOC) -> list[ClassifiedLink]:
    """Adapter-supplied URL lists (tweet entities.urls, telegram post links):
    the payload already curated them, so every http(s) URL is kept — no
    anchor-in-content keep rule — cleaned, deduped, classified
    official/file, and capped. ``is_official``/``is_file`` keep their pure
    column meanings (same as classify_links)."""
    own = {clean_url(u) for u in self_urls if u}
    kept: list[ClassifiedLink] = []
    seen: set[str] = set()
    for url in urls:
        if len(kept) >= max_links:
            break
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        cleaned = clean_url(url)
        if cleaned in own or cleaned in seen:
            continue
        seen.add(cleaned)
        kept.append(ClassifiedLink(
            url=cleaned, anchor_text=None, is_file=is_file_url(cleaned),
            is_official=is_official_domain(_host(cleaned), official_domains)))
    return kept


def collect_links(raw: bytes | str, *, base_url: str | None,
                  content_text: str, self_urls: Sequence[str | None],
                  official_domains: Sequence[str],
                  max_links: int = MAX_LINKS_PER_DOC) -> list[ClassifiedLink]:
    """extract + classify in one call (the shape pipeline & backfill use)."""
    pairs = extract_anchors(raw, base_url)
    return classify_links(
        pairs, content_text=content_text, self_urls=self_urls,
        official_domains=official_domains, max_links=max_links)
