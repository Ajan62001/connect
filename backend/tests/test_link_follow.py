"""Selective auto-follow: priority, caps, depth-1, dedup resolution,
published_at hint — all against a canned in-memory fetcher (no network)."""

from __future__ import annotations


from canned_web import FakeFetcher, html_page, pdf_page
from connect.domain.models import DiscoveredItem, DocumentLink
from connect.ingestion.link_follow import select_candidates
from connect.ingestion.rss_poller import RssPoller
from connect.storage import documents as doc_dao
from connect.storage import links as link_dao
from connect.storage import sources as source_dao

PARENT_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12690"
PDF_TWIN_URL = "https://www.rbi.org.in/rdocs/notification/PDFs/CIRC12690.PDF"
GAZETTE_URL = "https://egazette.gov.in/WriteReadData/2026/9001.pdf"


def _links(conn, doc_id) -> list[DocumentLink]:
    return link_dao.list_for_document(conn, doc_id)


def _edges(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT src_type, src_id, dst_type, dst_id, provenance_document_id,"
        " grade FROM edge WHERE relation = 'links_to' ORDER BY id").fetchall()]


# --- the flagship case: RBI circular page -> PDF twin ----------------------------


async def test_rbi_pdf_twin_followed_and_edge_written(container):
    fetcher = FakeFetcher({
        PARENT_URL: html_page(
            "Review of Risk Weights",
            f'<p><a href="{PDF_TWIN_URL}">Download the circular</a></p>'),
        PDF_TWIN_URL: pdf_page(
            "RBI/2026-27/45 Review of Risk Weights on Microfinance Loans. "
            "Banks shall assign a risk weight of one hundred percent."),
    })
    container.pipeline.fetcher = fetcher

    result = await container.pipeline.ingest_url(PARENT_URL)
    parent = result.document

    links = _links(container.db, parent.id)
    assert len(links) == 1
    link = links[0]
    assert link.is_file and link.is_official
    assert link.status == "fetched"
    assert link.resolved_document_id is not None

    child = doc_dao.get(container.db, link.resolved_document_id)
    assert child is not None
    assert child.media_type == "pdf"
    assert child.source_id is None  # lineage lives in the edge + link row

    edges = _edges(container.db)
    assert edges == [("document", parent.id, "document", child.id,
                      parent.id, 1)]


# --- priority & cap ---------------------------------------------------------------


def _mk_link(i: int, url: str, *, official: bool = False,
             file: bool = False, status: str = "not_followed") -> DocumentLink:
    return DocumentLink(id=i, document_id=1, url=url, is_official=official,
                        is_file=file, status=status)


def test_select_candidates_official_first_then_same_domain_files():
    parent_url = "https://www.thehindu.com/news/national/article123.ece"
    links = [
        _mk_link(1, "https://www.thehindu.com/docs/report.pdf", file=True),
        _mk_link(2, "https://egazette.gov.in/WriteReadData/1.pdf",
                 official=True, file=True),
        _mk_link(3, "https://pib.gov.in/PressReleasePage.aspx?PRID=1",
                 official=True),
        _mk_link(4, "https://example.com/elsewhere.pdf", file=True),
        _mk_link(5, "https://www.thehindu.com/other-article.ece"),
        _mk_link(6, "https://prsindia.org/billtrack/x", official=True,
                 status="fetched"),  # already done — not a candidate
    ]
    picked = select_candidates(links, parent_url=parent_url, max_per_doc=5)
    # officials in document order, then the same-domain file; cross-domain
    # non-official files and plain content links are never auto-followed
    assert [l.id for l in picked] == [2, 3, 1]

    picked = select_candidates(links, parent_url=parent_url, max_per_doc=2)
    assert [l.id for l in picked] == [2, 3]

    # no parent URL (e.g. uploaded file): only officials qualify
    picked = select_candidates(links, parent_url=None, max_per_doc=5)
    assert [l.id for l in picked] == [2, 3]


async def test_per_doc_follow_cap_leaves_rest_not_followed(container):
    o1 = "https://pib.gov.in/PressReleasePage.aspx?PRID=1"
    o2 = "https://egazette.gov.in/WriteReadData/2026/2.pdf"
    fetcher = FakeFetcher({
        PARENT_URL: html_page(
            "Circular with many references",
            f'<p><a href="{o1}">first official reference</a>'
            f' and <a href="{o2}">second official reference</a>'
            f' and <a href="{PDF_TWIN_URL}">the circular PDF</a></p>'),
        o1: html_page("PIB press release on the framework revision",
                      "<p>The ministry issued a detailed clarification about "
                      "the revised supervisory thresholds and timelines.</p>"),
        o2: pdf_page("Gazette notification 9001 of 2026 about thresholds."),
        PDF_TWIN_URL: pdf_page("Circular PDF body text for the revision."),
    })
    container.pipeline.fetcher = fetcher
    container.pipeline.link_follow_max_per_doc = 2

    result = await container.pipeline.ingest_url(PARENT_URL)
    statuses = {l.url: l.status for l in _links(container.db,
                                                result.document.id)}
    # candidate order: o1, o2 (cross-domain officials), then the same-domain
    # PDF twin; cap=2 follows the first two, the rest stays manually fetchable
    assert statuses[o1] == "fetched"
    assert statuses[o2] == "fetched"
    assert statuses[PDF_TWIN_URL] == "not_followed"
    assert PDF_TWIN_URL not in fetcher.calls


# --- depth 1: no recursion ---------------------------------------------------------


async def test_followed_documents_are_never_auto_followed_from(container):
    child_url = "https://pib.gov.in/PressReleasePage.aspx?PRID=77"
    grandchild_url = "https://egazette.gov.in/WriteReadData/2026/777.pdf"
    fetcher = FakeFetcher({
        PARENT_URL: html_page(
            "Notification referencing PIB",
            f'<p>See the <a href="{child_url}">press release</a>.</p>'),
        child_url: html_page(
            "Press release",
            f'<p>The <a href="{grandchild_url}">gazette notification</a> '
            f'has the operative text of the amendment.</p>'),
        grandchild_url: pdf_page("Gazette text."),
    })
    container.pipeline.fetcher = fetcher

    result = await container.pipeline.ingest_url(PARENT_URL)

    assert grandchild_url not in fetcher.calls  # depth 1: never recursed
    parent_links = _links(container.db, result.document.id)
    child_id = parent_links[0].resolved_document_id
    assert child_id is not None
    # the child still got ITS links extracted and stored…
    child_links = _links(container.db, child_id)
    assert [l.url for l in child_links] == [grandchild_url]
    # …but they were not followed
    assert child_links[0].status == "not_followed"
    assert len(_edges(container.db)) == 1


# --- dedup & idempotence ------------------------------------------------------------


async def test_dedup_hit_resolves_to_existing_doc_and_writes_edge(container):
    url_a = "https://pib.gov.in/PressReleasePage.aspx?PRID=500"
    url_b = "https://pib.gov.in/newsite/erelease.aspx?relid=500"  # same content
    page = html_page("Press release 500",
                     "<p>The cabinet approved the semiconductor incentive "
                     "scheme with an outlay decided earlier this year.</p>")
    fetcher = FakeFetcher({
        url_a: page,
        url_b: page,
        PARENT_URL: html_page(
            "Notification citing PIB",
            f'<p>Covered in the <a href="{url_b}">press release</a>.</p>'),
    })
    container.pipeline.fetcher = fetcher

    first = await container.pipeline.ingest_url(url_a)
    assert first.created

    result = await container.pipeline.ingest_url(PARENT_URL)
    link = _links(container.db, result.document.id)[0]
    # url_b was fetched, its content hash matched the existing document:
    # the link resolves to the EXISTING doc and the edge is still written
    assert url_b in fetcher.calls
    assert link.status == "fetched"
    assert link.resolved_document_id == first.document.id
    assert _edges(container.db) == [
        ("document", result.document.id, "document", first.document.id,
         result.document.id, 1)]


async def test_known_url_is_not_refetched(container):
    child_url = "https://egazette.gov.in/WriteReadData/2026/42.pdf"
    fetcher = FakeFetcher({
        child_url: pdf_page("Gazette notification number forty-two text."),
        PARENT_URL: html_page(
            "Notification",
            f'<p>The <a href="{child_url}">gazette notification</a> '
            f'applies.</p>'),
    })
    container.pipeline.fetcher = fetcher

    existing = await container.pipeline.ingest_url(child_url)
    result = await container.pipeline.ingest_url(PARENT_URL)

    assert fetcher.calls.count(child_url) == 1  # resolved by URL, no re-fetch
    link = _links(container.db, result.document.id)[0]
    assert link.status == "fetched"
    assert link.resolved_document_id == existing.document.id


# --- failure isolation ---------------------------------------------------------------


async def test_follow_failure_recorded_never_raises(container):
    dead_url = "https://egazette.gov.in/WriteReadData/2026/404.pdf"
    fetcher = FakeFetcher({
        PARENT_URL: html_page(
            "Notification with a dead link",
            f'<p>The <a href="{dead_url}">gazette notification</a> '
            f'was published.</p>'),
        # dead_url intentionally absent -> FetchError
    })
    container.pipeline.fetcher = fetcher

    result = await container.pipeline.ingest_url(PARENT_URL)  # must not raise
    assert result.created

    link = _links(container.db, result.document.id)[0]
    assert link.status == "failed"
    assert "404" in (link.error or "")
    assert link.resolved_document_id is None
    assert _edges(container.db) == []


# --- published_at hint ----------------------------------------------------------------


async def test_published_at_hint_wins_over_extracted_metadata(container):
    url = "https://pib.gov.in/PressReleasePage.aspx?PRID=2026001"
    ctype, body = html_page(
        "Scheme launch", "<p>The minister launched the scheme today with "
        "an outlay announced in the budget speech this February.</p>")
    body = body.replace(
        b"</title>",
        b'</title><meta property="article:published_time" '
        b'content="2025-11-30T10:00:00+05:30">')
    container.pipeline.fetcher = FakeFetcher({url: (ctype, body)})

    hint = "2026-06-10T07:30:00Z"
    result = await container.pipeline.ingest_url(url, published_at_hint=hint)
    assert result.document.published_at == hint


async def test_extracted_date_remains_the_fallback(container):
    url = "https://pib.gov.in/PressReleasePage.aspx?PRID=2026002"
    ctype, body = html_page(
        "Another scheme launch", "<p>A different ministry launched another "
        "scheme with district-level pilots starting next month.</p>")
    body = body.replace(
        b"</title>",
        b'</title><meta property="article:published_time" '
        b'content="2025-11-30T10:00:00+05:30">')
    container.pipeline.fetcher = FakeFetcher({url: (ctype, body)})

    result = await container.pipeline.ingest_url(url)  # no hint
    assert (result.document.published_at or "").startswith("2025-11-30")


async def test_rss_poller_passes_pubdate_hint(container):
    url = "https://pib.gov.in/PressReleasePage.aspx?PRID=2026003"
    ctype, body = html_page(
        "Feed item page", "<p>Yet another release whose embedded metadata "
        "would otherwise win over the feed pubDate.</p>")
    body = body.replace(
        b"</title>",
        b'</title><meta property="article:published_time" '
        b'content="2025-11-30T10:00:00+05:30">')
    container.pipeline.fetcher = FakeFetcher({url: (ctype, body)})

    source = source_dao.insert(
        container.db, name="PIB test feed", type_="rss",
        config={"feed_url": "https://pib.gov.in/rss.aspx"},
        credibility_tier=1)

    class StubAdapter:
        async def discover(self, config):
            return [DiscoveredItem(title="Scheme launched", url=url,
                                   published_at="2026-06-11T04:00:00Z")]

    poller = RssPoller(container.db, pipeline=container.pipeline,
                       adapter=StubAdapter())
    status = await poller.poll_source(source)
    assert status.startswith("ok: 1 new")

    document = doc_dao.get_by_url(container.db, url)
    assert document is not None
    assert document.published_at == "2026-06-11T04:00:00Z"
    assert document.title == "Scheme launched"
