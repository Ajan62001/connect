"""PDF -> text + metadata via pymupdf."""

from __future__ import annotations

from connect.ingestion.extract_html import Extracted, ExtractionError


def extract_pdf(data: bytes) -> Extracted:
    import fitz  # pymupdf — lazy heavy import

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001 — fitz raises various types
        raise ExtractionError(f"cannot open PDF: {e}") from e
    try:
        text = "\n".join(page.get_text() for page in doc).strip()
        meta = doc.metadata or {}
    finally:
        doc.close()
    if not text:
        raise ExtractionError("PDF contains no extractable text")
    return Extracted(
        text=text,
        title=(meta.get("title") or "").strip() or None,
        author=(meta.get("author") or "").strip() or None,
    )
