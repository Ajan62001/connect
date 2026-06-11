"""ingest_text end-to-end: row + blob + FTS + dedup (exact and near-dup)."""

from __future__ import annotations

from connect.storage import fts as fts_dao

TEXT = """
The finance ministry released the monthly GST collection figures showing
gross revenue of 1.8 lakh crore rupees for May, an eleven percent increase
over the same month last year. Officials attributed the growth to improved
compliance and higher imports. The compensation cess fund balance was also
published, alongside state-wise settlement numbers that show the largest
transfers going to Maharashtra, Karnataka and Tamil Nadu. Economists said
the trend, if sustained for two more quarters, would give the GST Council
room to consider the long-pending rate rationalisation exercise.
Revenue secretary officials briefed reporters that e-invoice coverage has
expanded to firms above the five crore turnover threshold, plugging input
tax credit leakages that had dogged the system since its launch. States
have asked the council to extend the compensation window, a demand the
centre has so far resisted, arguing that buoyant collections make the
support unnecessary. A ministerial panel on rate rationalisation is
expected to submit its report before the next council meeting, with
officials hinting that the twelve and eighteen percent slabs could be
merged. Industry bodies welcomed the numbers but flagged pending refunds
for exporters as a continuing concern, estimating the backlog at several
thousand crore rupees across sectors including textiles and pharma.
"""


def test_ingest_text_end_to_end(container):
    pipeline = container.pipeline
    result = pipeline.ingest_text(TEXT, title="GST collections rise")

    assert result.created is True
    doc = result.document
    assert doc.id >= 1
    assert doc.title == "GST collections rise"
    assert doc.media_type == "text"
    assert doc.enrichment_status == "pending"
    assert doc.content_hash

    # row is queryable
    row = container.db.execute(
        "SELECT simhash, raw_blob_path FROM document WHERE id=?",
        (doc.id,)).fetchone()
    assert row["simhash"] is not None

    # blob landed under data/blobs/<2-char>/<sha>
    assert container.blobs.exists(row["raw_blob_path"])
    assert container.blobs.get(row["raw_blob_path"]).decode("utf-8") == TEXT

    # FTS-searchable
    items, total = fts_dao.search_documents(container.db, "GST")
    assert total == 1
    assert items[0].id == doc.id
    assert items[0].snippet and "mark" in items[0].snippet


def test_snippet_html_escaped(container):
    """Snippets are rendered as HTML on the client (highlight marks) — any
    markup inside the document text itself must arrive escaped."""
    container.pipeline.ingest_text(
        "A report on zanzibar trade routes. <script>alert(1)</script> "
        "Spice exports & tariffs rose sharply this quarter.",
        title="xss probe")

    items, total = fts_dao.search_documents(container.db, "zanzibar")
    assert total == 1
    snip = items[0].snippet
    assert snip is not None
    assert "<mark>zanzibar</mark>" in snip
    assert "<script>" not in snip
    assert "&lt;script&gt;" in snip


def test_exact_dedup_returns_existing(container):
    pipeline = container.pipeline
    first = pipeline.ingest_text(TEXT, title="GST collections rise")
    again = pipeline.ingest_text(TEXT, title="different title, same body")

    assert again.created is False
    assert again.document.id == first.document.id
    count = container.db.execute("SELECT COUNT(*) FROM document").fetchone()[0]
    assert count == 1


def test_near_duplicate_gets_canonical_pointer(container):
    pipeline = container.pipeline
    first = pipeline.ingest_text(TEXT, title="GST collections rise")

    # syndicated copy: a couple of word-level edits on the same wire text
    edited = TEXT.replace("eleven percent", "twelve percent").replace(
        "Officials attributed", "Officials credited")
    second = pipeline.ingest_text(edited, title="GST mop-up grows")

    assert second.created is True  # stored — immutable snapshot discipline
    assert second.document.canonical_document_id == first.document.id
    assert second.document.enrichment_status == "skipped_dup"


def test_malformed_fts_query_does_not_crash(container):
    container.pipeline.ingest_text(TEXT, title="GST collections rise")
    items, total = fts_dao.search_documents(container.db, 'AND NOT "')
    assert total == 0 and items == []
