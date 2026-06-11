"""Shared canned-web helpers for link tests — no network, no model downloads."""

from __future__ import annotations

from connect.ingestion.fetcher import FetchError, FetchResult


class FakeFetcher:
    """url -> (content_type, bytes); records every fetch."""

    def __init__(self, pages: dict[str, tuple[str, bytes]]):
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str, *, ignore_robots: bool = False) -> FetchResult:
        self.calls.append(url)
        if url not in self.pages:
            raise FetchError(f"HTTP 404 for {url}")
        content_type, content = self.pages[url]
        return FetchResult(url=url, final_url=url, status_code=200,
                           content=content, content_type=content_type)


def html_page(title: str, body_html: str) -> tuple[str, bytes]:
    """A page with enough prose that trafilatura reliably extracts it."""
    page = f"""<html><head><title>{title}</title></head><body><article>
<h1>{title}</h1>
<p>The committee reviewed the framework for regulated entities and decided
to revise the applicable thresholds with effect from the next quarter,
citing supervisory data gathered over the previous review cycle.</p>
{body_html}
<p>Stakeholders were advised to submit implementation reports within sixty
days, and supervisors will verify compliance during the annual inspection
cycle that begins after the monsoon session concludes.</p>
</article></body></html>"""
    return ("text/html; charset=utf-8", page.encode("utf-8"))


def pdf_page(text: str) -> tuple[str, bytes]:
    import fitz  # pymupdf

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return ("application/pdf", data)
