"""Office formats -> text: .xlsx (openpyxl), .xls (xlrd), .docx (python-docx).

Gov sources link spreadsheets (RBI annexes, budget data) and Word files.
All three extractors return the same ``Extracted`` shape as extract_html /
extract_pdf and raise ``ExtractionError`` on failure — a bad spreadsheet
takes the exact failure path a bad PDF does.

Text shape (shared by all three): per sheet a ``## <sheet name>`` heading,
then rows as tab-joined cell strings (None -> '', trailing empties dropped,
fully-empty rows skipped). Numbers are kept verbatim — they feed numeric
claim-verification later. Caps keep pathological files bounded:
MAX_SHEETS sheets, MAX_ROWS rows per sheet/table, MAX_COLS columns; a
``[truncated: ...]`` note marks every cap that fired.

``detect_office_kind`` is the one routing rule (pipeline.ingest_url /
ingest_file both call it): content-type header and URL/filename extension
pick the kind, magic bytes break ties — a 'PK' zip can never be a legacy
.xls (mislabelled modern file), an OLE compound file (D0 CF 11 E0) can only
be legacy Excel here.
"""

from __future__ import annotations

from io import BytesIO
from urllib.parse import urlsplit

from connect.ingestion.extract_html import Extracted, ExtractionError

MAX_SHEETS = 10
MAX_ROWS = 500   # per sheet (xlsx/xls) / per table (docx)
MAX_COLS = 50

# detect_office_kind result -> document.media_type ('xlsx' covers xls too).
OFFICE_MEDIA_TYPES = {"xlsx": "xlsx", "xls": "xlsx", "docx": "docx"}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # OLE2 compound file (legacy Office)
_ZIP_MAGIC = b"PK"                # OOXML container (xlsx/docx)


# -- routing -------------------------------------------------------------------

def detect_office_kind(*, content_type: str | None, name: str | None,
                       data: bytes) -> str | None:
    """'xlsx' | 'xls' | 'docx' | None for a fetched/uploaded payload.

    ``name`` is a URL or a filename (query strings are ignored). The
    extension/content-type pick the kind; magic bytes only break ties,
    never initiate a match (a random zip must not become a spreadsheet).
    """
    ctype = (content_type or "").lower()
    path = urlsplit((name or "").lower()).path
    if path.endswith(".xlsx") or "spreadsheetml" in ctype:
        kind = "xlsx"
    elif path.endswith(".xls") or "ms-excel" in ctype:
        kind = "xls"
    elif path.endswith(".docx") or "wordprocessingml" in ctype:
        kind = "docx"
    else:
        return None
    if data.startswith(_OLE_MAGIC):
        return "xls"   # legacy file behind a modern label
    if data.startswith(_ZIP_MAGIC) and kind == "xls":
        return "xlsx"  # modern file behind a legacy label
    return kind


def extract_office(data: bytes, kind: str) -> Extracted:
    """Dispatch on a detect_office_kind result. For the zip-based twins the
    hinted library goes first; a 'PK' payload that it cannot open is retried
    with the sibling (extension/content-type picked the wrong OOXML twin)."""
    if kind == "xls":
        return extract_xls(data)
    primary, sibling = ((extract_xlsx, extract_docx) if kind == "xlsx"
                        else (extract_docx, extract_xlsx))
    try:
        return primary(data)
    except ExtractionError:
        if not data.startswith(_ZIP_MAGIC):
            raise
        return sibling(data)


# -- shared text shaping ---------------------------------------------------------

def _cell_str(value: object) -> str:
    """A cell as displayed: None -> '', integral floats lose the '.0' (XLS
    stores every number as float; '182000.0' would corrupt verbatim numbers)."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _emit_rows(parts: list[str], rows, label: str) -> int:
    """Append tab-joined rows to ``parts`` under the row/column caps;
    returns how many rows were emitted (headings alone are not content).

    ``rows`` yields sequences of already-stringified cells. Fully-empty rows
    are skipped and do not count against the row cap.
    """
    emitted = 0
    cols_truncated = False
    for row in rows:
        cells = list(row)
        if len(cells) > MAX_COLS:
            cells = cells[:MAX_COLS]
            cols_truncated = True
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue  # fully empty row
        if emitted >= MAX_ROWS:
            parts.append(
                f"[truncated: only first {MAX_ROWS} rows of {label} extracted]")
            break
        parts.append("\t".join(cells))
        emitted += 1
    if cols_truncated:
        parts.append(
            f"[truncated: only first {MAX_COLS} columns of {label} extracted]")
    return emitted


def _finish(parts: list[str], title: str | None, what: str,
            content_units: int) -> Extracted:
    """``content_units`` counts real content (data rows / paragraphs) — sheet
    headings alone must not pass for extractable text."""
    text = "\n".join(parts).strip()
    if not text or content_units == 0:
        raise ExtractionError(f"{what} contains no extractable text")
    return Extracted(text=text, title=(title or "").strip() or None)


# -- extractors -------------------------------------------------------------------

def extract_xlsx(data: bytes) -> Extracted:
    import openpyxl  # lazy heavy import

    try:
        workbook = openpyxl.load_workbook(
            BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 — openpyxl raises various types
        raise ExtractionError(f"cannot open XLSX: {e}") from e
    try:
        parts: list[str] = []
        rows_emitted = 0
        sheets = workbook.worksheets
        for sheet in sheets[:MAX_SHEETS]:
            parts.append(f"## {sheet.title}")
            rows_emitted += _emit_rows(
                parts,
                ((_cell_str(v) for v in row)
                 for row in sheet.iter_rows(values_only=True)),
                f"sheet '{sheet.title}'")
        if len(sheets) > MAX_SHEETS:
            parts.append(f"[truncated: only first {MAX_SHEETS} of "
                         f"{len(sheets)} sheets extracted]")
        title = workbook.properties.title
    finally:
        workbook.close()
    return _finish(parts, title, "XLSX", rows_emitted)


def extract_xls(data: bytes) -> Extracted:
    import xlrd  # lazy heavy import

    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as e:  # noqa: BLE001 — xlrd raises various types
        raise ExtractionError(f"cannot open XLS: {e}") from e
    parts: list[str] = []
    rows_emitted = 0
    for sheet in book.sheets()[:MAX_SHEETS]:
        parts.append(f"## {sheet.name}")
        rows_emitted += _emit_rows(
            parts,
            ((_cell_str(v) for v in sheet.row_values(rx))  # empty cell is ''
             for rx in range(sheet.nrows)),
            f"sheet '{sheet.name}'")
    if book.nsheets > MAX_SHEETS:
        parts.append(f"[truncated: only first {MAX_SHEETS} of "
                     f"{book.nsheets} sheets extracted]")
    # BIFF carries no usable title metadata
    return _finish(parts, None, "XLS", rows_emitted)


def extract_docx(data: bytes) -> Extracted:
    import docx  # python-docx — lazy heavy import

    try:
        document = docx.Document(BytesIO(data))
    except Exception as e:  # noqa: BLE001 — python-docx raises various types
        raise ExtractionError(f"cannot open DOCX: {e}") from e
    parts: list[str] = []
    content_units = 0
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
            content_units += 1
    for i, table in enumerate(document.tables, start=1):
        content_units += _emit_rows(
            parts,
            ((cell.text.strip() for cell in row.cells[:MAX_COLS + 1])
             for row in table.rows),
            f"table {i}")
    return _finish(parts, document.core_properties.title, "DOCX",
                   content_units)
