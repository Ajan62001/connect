"""Link extraction & classification — pure functions over a realistic
RBI-like fixture page (nav chrome, content links, a PDF twin, an official
external link, tracking params, mailto/js/self noise)."""

from __future__ import annotations

from connect.ingestion.links import (
    ClassifiedLink,
    classify_links,
    clean_url,
    collect_links,
    extract_anchors,
    is_official_domain,
    registrable_domain,
)
from connect.orchestration.config import DEFAULT_OFFICIAL_DOMAINS

PAGE_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=999"

FIXTURE_HTML = b"""
<html><head><title>Reserve Bank of India - Notifications</title></head>
<body>
<nav>
  <a href="/home.aspx">Home</a>
  <a href="/Scripts/AboutUsDisplay.aspx">About Us</a>
  <a href="javascript:openSitemap()">Sitemap</a>
</nav>
<div id="content">
  <h1>Review of Risk Weights on Microfinance Loans</h1>
  <p>Please refer to the
     <a href="/Scripts/BS_CircularIndexDisplay.aspx?Id=12345">circular on
     risk weights</a> issued earlier this year, which set out the
     framework for consumer credit exposures of commercial banks.</p>
  <p>As per the <a
     href="https://egazette.gov.in/WriteReadData/2026/123456.pdf">gazette
     notification</a> published last week, the revised weights apply
     from the first of April.</p>
  <p>The announcement was also covered by the
     <a href="https://pib.gov.in/PressReleasePage.aspx?PRID=2099999&amp;utm_source=feed&amp;utm_medium=rss&amp;gclid=abc123">Press
     Information Bureau</a> in its daily bulletin.</p>
  <p><a href="/rdocs/notification/PDFs/NT123CIRC2026.PDF">Download the
     circular (PDF)</a></p>
  <p>Duplicate mention of the <a
     href="/Scripts/BS_CircularIndexDisplay.aspx?Id=12345">circular on
     risk weights</a> again.</p>
  <p>Queries may be sent to <a
     href="mailto:helpdoc@rbi.org.in">helpdoc@rbi.org.in</a>.</p>
  <p><a href="#top">Back to top</a></p>
  <p><a href="https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=999">
     Permalink</a></p>
  <p>Commentary: <a href="https://example.com/blog/why-rbi-did-this">a
     sharp take on the move</a>.</p>
</div>
</body></html>
"""

# What trafilatura-style extraction would yield: content anchors appear in
# the text, nav chrome does not.
CONTENT_TEXT = """Review of Risk Weights on Microfinance Loans
Please refer to the circular on risk weights issued earlier this year,
which set out the framework for consumer credit exposures of commercial banks.
As per the gazette notification published last week, the revised weights
apply from the first of April.
The announcement was also covered by the Press Information Bureau in its
daily bulletin.
Download the circular (PDF)
Duplicate mention of the circular on risk weights again.
Queries may be sent to helpdoc@rbi.org.in.
"""


def _collect(max_links: int = 50) -> list[ClassifiedLink]:
    return collect_links(
        FIXTURE_HTML, base_url=PAGE_URL, content_text=CONTENT_TEXT,
        self_urls=(PAGE_URL,), official_domains=DEFAULT_OFFICIAL_DOMAINS,
        max_links=max_links)


def test_extract_anchors_resolves_dedupes_and_drops_noise():
    pairs = extract_anchors(FIXTURE_HTML, PAGE_URL)
    urls = [u for u, _ in pairs]
    # relative URLs resolved against the page URL
    assert "https://www.rbi.org.in/home.aspx" in urls
    # mailto / javascript / same-page fragments dropped
    assert not any(u.startswith(("mailto:", "javascript:")) for u in urls)
    assert not any("#" in u for u in urls)
    # duplicate href appears once
    circ = "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx?Id=12345"
    assert urls.count(circ) == 1
    # tracking params stripped, real params kept
    pib = next(u for u in urls if "pib.gov.in" in u)
    assert pib == "https://pib.gov.in/PressReleasePage.aspx?PRID=2099999"


def test_classification_keep_rules():
    kept = {l.url: l for l in _collect()}

    # (a) content link: anchor text appears in extracted text
    circ = kept[
        "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx?Id=12345"]
    assert circ.is_official and not circ.is_file
    assert circ.anchor_text == "circular on risk weights"

    # (b) file links: same-domain PDF twin (case-insensitive extension)
    pdf = kept["https://www.rbi.org.in/rdocs/notification/PDFs/NT123CIRC2026.PDF"]
    assert pdf.is_file and pdf.is_official

    # (c) cross-domain official links
    gazette = kept["https://egazette.gov.in/WriteReadData/2026/123456.pdf"]
    assert gazette.is_file and gazette.is_official
    assert "https://pib.gov.in/PressReleasePage.aspx?PRID=2099999" in kept

    # nav chrome dropped: same-domain + anchor not in content
    assert "https://www.rbi.org.in/home.aspx" not in kept
    assert "https://www.rbi.org.in/Scripts/AboutUsDisplay.aspx" not in kept
    # self URL dropped
    assert PAGE_URL not in kept
    # non-official external link whose anchor is not in content dropped
    assert "https://example.com/blog/why-rbi-did-this" not in kept

    assert len(kept) == 4


def test_per_document_cap():
    assert len(_collect(max_links=2)) == 2

    # synthetic flood: 60 cross-domain official links -> capped at 50
    flood = [(f"https://egazette.gov.in/doc/{i}", f"doc {i}")
             for i in range(60)]
    kept = classify_links(
        flood, content_text="", self_urls=(PAGE_URL,),
        official_domains=DEFAULT_OFFICIAL_DOMAINS)
    assert len(kept) == 50


def test_anchor_in_content_requires_min_length():
    pairs = [("https://example.com/x", "ab"),     # too short
             ("https://example.com/y", "April")]  # in content
    kept = classify_links(
        pairs, content_text=CONTENT_TEXT, self_urls=(),
        official_domains=DEFAULT_OFFICIAL_DOMAINS)
    assert [l.url for l in kept] == ["https://example.com/y"]


def test_anchor_match_is_word_bounded_not_substring():
    pairs = [("https://example.com/act", "Act"),
             ("https://example.com/go", "Gold")]
    # 'act' appears only INSIDE 'exact'/'transactions'; 'gold' inside 'goldsmith'
    kept = classify_links(
        pairs, content_text="The exact transactions involved a goldsmith.",
        self_urls=(), official_domains=DEFAULT_OFFICIAL_DOMAINS)
    assert kept == []

    kept = classify_links(
        pairs, content_text="Under the Act, bullion imports were restricted.",
        self_urls=(), official_domains=DEFAULT_OFFICIAL_DOMAINS)
    assert [l.url for l in kept] == ["https://example.com/act"]


def test_unparseable_html_yields_no_links():
    assert extract_anchors(b"", PAGE_URL) == []


def test_extract_without_base_url_keeps_only_absolute():
    pairs = extract_anchors(FIXTURE_HTML, None)
    assert all(u.startswith("http") for u, _ in pairs)
    assert not any("/home.aspx" == u for u, _ in pairs)


def test_registrable_domain_and_official_match():
    assert registrable_domain("www.rbi.org.in") == "rbi.org.in"
    assert registrable_domain("pib.gov.in") == "pib.gov.in"
    assert registrable_domain("static.pib.gov.in") == "pib.gov.in"
    assert registrable_domain("m.thehindu.com") == "thehindu.com"
    assert is_official_domain("pib.gov.in", DEFAULT_OFFICIAL_DOMAINS)
    assert is_official_domain("www.rbi.org.in", DEFAULT_OFFICIAL_DOMAINS)
    assert not is_official_domain("notgov.in", DEFAULT_OFFICIAL_DOMAINS)
    assert not is_official_domain("example.com", DEFAULT_OFFICIAL_DOMAINS)


def test_clean_url_strips_fragment_and_tracking_only():
    assert clean_url("https://x.in/a?utm_source=rss&id=5&fbclid=z#frag") == \
        "https://x.in/a?id=5"
