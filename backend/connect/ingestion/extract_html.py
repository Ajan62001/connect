"""HTML -> main text + metadata via trafilatura."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


class ExtractionError(Exception):
    """No usable text could be extracted."""


@dataclass(frozen=True)
class Extracted:
    text: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    language: str | None = None


def extract_html(html: str | bytes, url: str | None = None) -> Extracted:
    """Main-content extraction with metadata; raises ExtractionError when
    trafilatura finds nothing usable."""
    import trafilatura  # lazy heavy import

    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    raw = trafilatura.extract(
        html, url=url, output_format="json", with_metadata=True,
        include_comments=False)
    if not raw:
        raise ExtractionError("trafilatura extracted no content")
    data = json.loads(raw)
    text = (data.get("text") or "").strip()
    if not text:
        raise ExtractionError("trafilatura extracted empty text")
    return Extracted(
        text=text,
        title=data.get("title") or None,
        author=data.get("author") or None,
        published_at=data.get("date") or None,
        language=data.get("language") or None,
    )
