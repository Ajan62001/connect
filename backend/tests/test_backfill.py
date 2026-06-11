"""backfill_links tool: extracts links for pre-Phase-0.5 HTML documents
(zero document_link rows) from their stored raw blobs."""

from __future__ import annotations

import asyncio

from canned_web import FakeFetcher, html_page
from connect.storage import links as link_dao
from connect.tools import backfill_links

PARENT_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=31"
GAZETTE_URL = "https://egazette.gov.in/WriteReadData/2026/31.pdf"


def test_backfill_extracts_links_for_legacy_documents(container, settings):
    # Ingest a doc as if it predated link extraction: extract normally,
    # then wipe its link rows (the legacy state the tool targets).
    container.pipeline.fetcher = FakeFetcher({
        PARENT_URL: html_page(
            "Legacy notification",
            f'<p>The <a href="{GAZETTE_URL}">gazette notification</a> '
            f'carries the operative text.</p>'),
        GAZETTE_URL: ("application/pdf", b"ignored"),
    })
    container.pipeline.link_follow_enabled = False
    result = asyncio.run(container.pipeline.ingest_url(PARENT_URL))
    doc_id = result.document.id
    with container.db:
        container.db.execute("DELETE FROM document_link")
    assert link_dao.list_for_document(container.db, doc_id) == []

    counts = asyncio.run(backfill_links.run(settings, follow=False))

    assert counts["scanned"] == 1
    assert counts["links_stored"] == 1
    assert counts["followed_ok"] == counts["followed_failed"] == 0
    links = link_dao.list_for_document(container.db, doc_id)
    assert [l.url for l in links] == [GAZETTE_URL]
    assert links[0].status == "not_followed"

    # second run is a no-op: the document now HAS link rows
    counts = asyncio.run(backfill_links.run(settings, follow=False))
    assert counts["scanned"] == 0 and counts["links_stored"] == 0
