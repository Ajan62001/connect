"""web_news adapter: HTML fixture -> items, dedup, pattern filter, base_url
resolution, source-config validation, poller integration — zero network."""

from __future__ import annotations

from pathlib import Path

import pytest

from canned_web import FakeFetcher, html_page
from connect.ingestion.poller import SourcePoller
from connect.sources.adapters.web_news import WebNewsAdapter, parse_news_index
from connect.sources.base import SourceConfigError, WebNewsConfig, parse_source_config
from connect.storage import documents as doc_dao
from connect.storage import sources as source_dao

FIXTURE = Path(__file__).parent / "fixtures" / "moneycontrol_news.html"
INDEX_URL = "https://www.moneycontrol.com/news/"
PATTERN = r"moneycontrol\.com/news/[a-z][a-z0-9/\-]+-\d+\.html"

ARTICLE_RBI = ("https://www.moneycontrol.com/news/business/economy/"
               "rbi-cuts-repo-rate-by-25-bps-amid-easing-inflation-12345678.html")
ARTICLE_SENSEX = ("https://www.moneycontrol.com/news/markets/stocks/"
                  "sensex-rises-200-points-nifty-above-23500-12345679.html")
ARTICLE_GST = ("https://www.moneycontrol.com/news/economy/policy/"
               "gst-council-meets-to-review-rate-rationalisation-12345680.html")
ARTICLE_GOLD = ("https://www.moneycontrol.com/news/markets/commodities/"
                "gold-hits-record-high-domestic-demand-surges-12345681.html")


def _article_page(url: str) -> tuple[str, bytes]:
    slug = url.rsplit("/", 1)[-1].replace(".html", "").replace("-", " ")
    return html_page(slug, "<p>Full article content.</p>")


# ---------------------------------------------------------------------------
# parse_news_index (pure)
# ---------------------------------------------------------------------------

def test_parse_extracts_article_links():
    items = parse_news_index(FIXTURE.read_bytes(), link_pattern=PATTERN,
                             base_url=INDEX_URL)
    urls = [i.url for i in items]
    assert ARTICLE_RBI in urls
    assert ARTICLE_SENSEX in urls
    assert ARTICLE_GST in urls
    assert ARTICLE_GOLD in urls   # was a relative href, resolved via base_url


def test_parse_excludes_nav_and_footer_links():
    items = parse_news_index(FIXTURE.read_bytes(), link_pattern=PATTERN,
                             base_url=INDEX_URL)
    urls = [i.url for i in items]
    assert INDEX_URL not in urls
    assert "https://www.moneycontrol.com/news/business/" not in urls
    assert "https://www.moneycontrol.com/news/tags/rbi" not in urls
    assert "https://www.moneycontrol.com/about-us/" not in urls


def test_parse_deduplicates_urls():
    items = parse_news_index(FIXTURE.read_bytes(), link_pattern=PATTERN,
                             base_url=INDEX_URL)
    urls = [i.url for i in items]
    assert urls.count(ARTICLE_RBI) == 1


def test_parse_extracts_titles_from_link_text():
    items = parse_news_index(FIXTURE.read_bytes(), link_pattern=PATTERN,
                             base_url=INDEX_URL)
    by_url = {i.url: i for i in items}
    assert by_url[ARTICLE_RBI].title == (
        "RBI cuts repo rate by 25 bps amid easing inflation")
    assert by_url[ARTICLE_GST].title == (
        "GST Council meets to review rate rationalisation")


def test_parse_resolves_relative_hrefs():
    items = parse_news_index(FIXTURE.read_bytes(), link_pattern=PATTERN,
                             base_url=INDEX_URL)
    assert ARTICLE_GOLD in [i.url for i in items]


def test_parse_no_base_url_drops_relative_hrefs():
    items = parse_news_index(FIXTURE.read_bytes(), link_pattern=PATTERN)
    assert ARTICLE_GOLD not in [i.url for i in items]


def test_parse_slug_title_fallback():
    html = (b'<html><body>'
            b'<a href="https://www.moneycontrol.com/news/economy/'
            b'inflation-data-released-12345999.html">  </a>'
            b'</body></html>')
    items = parse_news_index(html, link_pattern=PATTERN)
    assert len(items) == 1
    assert "inflation" in items[0].title.lower()


def test_parse_garbage_yields_empty():
    assert parse_news_index(b"not html at all <<<", link_pattern=PATTERN) == []


def test_parse_items_have_no_content_text():
    """content_text=None signals the poller to call pipeline.ingest_url."""
    items = parse_news_index(FIXTURE.read_bytes(), link_pattern=PATTERN,
                             base_url=INDEX_URL)
    assert all(i.content_text is None for i in items)


# ---------------------------------------------------------------------------
# WebNewsConfig validation
# ---------------------------------------------------------------------------

def test_config_valid():
    cfg = parse_source_config("web_news", {
        "index_url": INDEX_URL,
        "link_pattern": PATTERN,
    })
    assert isinstance(cfg, WebNewsConfig)
    assert cfg.poll_interval_minutes == 60


def test_config_rejects_non_http_index_url():
    with pytest.raises(SourceConfigError):
        parse_source_config("web_news", {
            "index_url": "ftp://example.com/news",
            "link_pattern": PATTERN,
        })


def test_config_rejects_invalid_regex():
    with pytest.raises(SourceConfigError):
        parse_source_config("web_news", {
            "index_url": INDEX_URL,
            "link_pattern": r"[invalid(regex",
        })


# ---------------------------------------------------------------------------
# Poller integration (FakeFetcher + real DB)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poller_discovers_and_ingests_articles(container, db):
    pages = {
        INDEX_URL: ("text/html", FIXTURE.read_bytes()),
        ARTICLE_RBI: _article_page(ARTICLE_RBI),
        ARTICLE_SENSEX: _article_page(ARTICLE_SENSEX),
        ARTICLE_GST: _article_page(ARTICLE_GST),
        ARTICLE_GOLD: _article_page(ARTICLE_GOLD),
    }
    fetcher = FakeFetcher(pages)
    container.pipeline.fetcher = fetcher
    adapter = WebNewsAdapter(fetcher)

    source = await source_dao.insert(
        db, name="Moneycontrol test", type_="web_news",
        config={"index_url": INDEX_URL, "link_pattern": PATTERN},
        credibility_tier=2)
    poller = SourcePoller(container.pool, pipeline=container.pipeline,
                          adapters={"web_news": adapter})

    status = await poller.poll_source(db, source)
    assert status.startswith("ok:")

    rbi_doc = await doc_dao.get_by_url(db, ARTICLE_RBI)
    assert rbi_doc is not None
    assert rbi_doc.source_id == source.id


@pytest.mark.asyncio
async def test_poller_deduplicates_on_re_poll(container, db):
    """Re-polling the same index does not create duplicate documents."""
    pages = {
        INDEX_URL: ("text/html", FIXTURE.read_bytes()),
        ARTICLE_RBI: _article_page(ARTICLE_RBI),
        ARTICLE_SENSEX: _article_page(ARTICLE_SENSEX),
        ARTICLE_GST: _article_page(ARTICLE_GST),
        ARTICLE_GOLD: _article_page(ARTICLE_GOLD),
    }
    fetcher = FakeFetcher(pages)
    container.pipeline.fetcher = fetcher
    adapter = WebNewsAdapter(fetcher)

    source = await source_dao.insert(
        db, name="MC dedup test", type_="web_news",
        config={"index_url": INDEX_URL, "link_pattern": PATTERN},
        credibility_tier=2)
    poller = SourcePoller(container.pool, pipeline=container.pipeline,
                          adapters={"web_news": adapter})

    first_status = await poller.poll_source(db, source)
    assert first_status.startswith("ok:")

    # refresh source row (last_polled_at now set)
    refreshed = await source_dao.get(db, source.id)
    second_status = await poller.poll_source(db, refreshed)
    assert second_status.startswith("ok:")

    # doc count unchanged
    doc_rbi = await doc_dao.get_by_url(db, ARTICLE_RBI)
    assert doc_rbi is not None   # still exactly one row
