"""Historical source backfill: the four discovery strategies (sitemap /
pagination / wayback / manual) over FakeFetcher fixtures, the orchestrator
end-to-end through the real ingest pipeline + DB, idempotency, domain
attribution, and the API endpoint contract. Zero network."""

from __future__ import annotations

import json

from canned_web import FakeFetcher, html_page
from connect.sources import backfill as bf
from connect.sources.attribution import make_resolver
from connect.sources.backfill.pagination import discover_paginated_urls
from connect.sources.backfill.sitemap import discover_sitemap_urls
from connect.sources.backfill.wayback import (
    build_cdx_url,
    discover_wayback_urls,
)
from connect.storage import documents as doc_dao
from connect.storage import sources as source_dao

BASE = "https://news.example.com"
PATTERN = r"news\.example\.com/article/\d+"


def art(i: int) -> str:
    return f"{BASE}/article/{i}"


SITEMAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url><loc>{art(101)}</loc>
    <news:news><news:publication_date>2024-01-15</news:publication_date></news:news>
  </url>
  <url><loc>{art(102)}</loc><lastmod>2024-02-20</lastmod></url>
  <url><loc>{art(103)}</loc><lastmod>2023-11-01</lastmod></url>
  <url><loc>{BASE}/about</loc></url>
</urlset>""".encode()

ROBOTS = (f"User-agent: *\nDisallow: /admin\n"
          f"Sitemap: {BASE}/sitemap.xml\n").encode()


def _site_pages() -> dict[str, tuple[str, bytes]]:
    pages = {
        f"{BASE}/robots.txt": ("text/plain", ROBOTS),
        f"{BASE}/sitemap.xml": ("application/xml", SITEMAP_XML),
    }
    for n in (101, 102, 103):
        pages[art(n)] = html_page(f"Article {n}", f"<p>Body of article {n}.</p>")
    return pages


def _listing(article_ids: list[int]) -> tuple[str, bytes]:
    links = "".join(f'<a href="{art(i)}">Article {i}</a>' for i in article_ids)
    return ("text/html; charset=utf-8",
            f"<html><body>{links}</body></html>".encode())


# ---------------------------------------------------------------------------
# sitemap strategy
# ---------------------------------------------------------------------------

async def test_sitemap_discovers_articles_in_window():
    fetcher = FakeFetcher(_site_pages())
    cands = await discover_sitemap_urls(
        fetcher, root_urls=[f"{BASE}/sitemap.xml"],
        start="2024-01-01", end="2024-03-01", link_pattern=PATTERN)
    urls = [c.url for c in cands]
    assert art(101) in urls and art(102) in urls
    assert art(103) not in urls            # older than start -> filtered
    assert f"{BASE}/about" not in urls      # fails link_pattern
    by_url = {c.url: c for c in cands}
    assert by_url[art(101)].published_at == "2024-01-15"   # news pub date


async def test_sitemap_index_recurses_to_child():
    index_xml = (f'<?xml version="1.0"?><sitemapindex '
                 f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 f'<sitemap><loc>{BASE}/sitemap.xml</loc></sitemap>'
                 f'</sitemapindex>').encode()
    pages = _site_pages()
    pages[f"{BASE}/sitemap_index.xml"] = ("application/xml", index_xml)
    fetcher = FakeFetcher(pages)
    cands = await discover_sitemap_urls(
        fetcher, root_urls=[f"{BASE}/sitemap_index.xml"], link_pattern=PATTERN)
    assert art(101) in [c.url for c in cands]   # found via the child urlset


# ---------------------------------------------------------------------------
# pagination strategy
# ---------------------------------------------------------------------------

async def test_pagination_walks_until_no_new_links():
    index = f"{BASE}/economy"
    pages = {
        index: _listing([101, 102]),
        f"{index}?page=2": _listing([103, 104]),
        f"{index}?page=3": _listing([103, 104]),   # repeat -> exhausted
    }
    fetcher = FakeFetcher(pages)
    cands = await discover_paginated_urls(
        fetcher, index_url=index, link_pattern=PATTERN, max_pages=5)
    assert [c.url for c in cands] == [art(101), art(102), art(103), art(104)]
    assert f"{index}?page=4" not in fetcher.calls   # stopped early


async def test_pagination_stops_on_fetch_error():
    index = f"{BASE}/economy"
    fetcher = FakeFetcher({index: _listing([101])})  # page 2 -> 404
    cands = await discover_paginated_urls(
        fetcher, index_url=index, link_pattern=PATTERN, max_pages=5)
    assert [c.url for c in cands] == [art(101)]


# ---------------------------------------------------------------------------
# wayback strategy
# ---------------------------------------------------------------------------

async def test_wayback_enumerates_from_cdx():
    cdx_url = build_cdx_url(BASE, start="2024-01-01", end="2024-03-31",
                            limit=1000)
    cdx_body = json.dumps([
        ["original", "timestamp", "statuscode"],
        [art(101), "20240115120000", "200"],
        [art(101), "20240116120000", "200"],   # dup url
        [art(102), "20240220090000", "200"],
    ]).encode()
    fetcher = FakeFetcher({cdx_url: ("application/json", cdx_body)})
    cands = await discover_wayback_urls(
        fetcher, target_url=BASE, start="2024-01-01", end="2024-03-31")
    assert [c.url for c in cands] == [art(101), art(102)]
    assert cands[0].published_at == "2024-01-15"


async def test_wayback_empty_on_archive_failure():
    fetcher = FakeFetcher({})   # CDX URL 404s
    assert await discover_wayback_urls(fetcher, target_url=BASE) == []


# ---------------------------------------------------------------------------
# orchestrator end-to-end (FakeFetcher + real pipeline + DB)
# ---------------------------------------------------------------------------

async def _web_news_source(db):
    return await source_dao.insert(
        db, name="news.example backfill", type_="web_news",
        config={"index_url": f"{BASE}/economy", "link_pattern": PATTERN},
        credibility_tier=2)


async def test_run_backfill_sitemap_ingests_window(container, db):
    fetcher = FakeFetcher(_site_pages())
    container.pipeline.fetcher = fetcher
    source = await _web_news_source(db)

    result = await bf.run_backfill(
        db, source=source, pipeline=container.pipeline, fetcher=fetcher,
        methods=["sitemap"], start="2024-01-01", end="2024-03-01")

    assert result["new"] == 2 and result["errors"] == 0
    doc = await doc_dao.get_by_url(db, art(101))
    assert doc is not None and doc.source_id == source.id
    cur = await db.execute(
        "SELECT origin FROM document WHERE url = %s", (art(101),))
    assert (await cur.fetchone())["origin"] == "backfill"


async def test_run_backfill_is_idempotent_on_rerun(container, db):
    fetcher = FakeFetcher(_site_pages())
    container.pipeline.fetcher = fetcher
    source = await _web_news_source(db)
    common = dict(source=source, pipeline=container.pipeline, fetcher=fetcher,
                  methods=["sitemap"], start="2024-01-01", end="2024-03-01")

    first = await bf.run_backfill(db, **common)
    assert first["new"] == 2
    second = await bf.run_backfill(db, **common)
    assert second["new"] == 0 and second["dups"] == 2   # all already known


async def test_run_backfill_manual_with_attribution(container, db):
    fetcher = FakeFetcher(_site_pages())
    container.pipeline.fetcher = fetcher
    # a source that owns the news.example.com domain at tier 1
    owner = await source_dao.insert(
        db, name="news.example (registered)", type_="rss",
        config={"feed_url": f"{BASE}/rss"}, credibility_tier=1)
    trigger = await source_dao.insert(
        db, name="manual trigger", type_="manual", config={},
        credibility_tier=4)
    resolver = make_resolver({"news.example.com": (owner.id, 1)})

    result = await bf.run_backfill(
        db, source=trigger, pipeline=container.pipeline, fetcher=fetcher,
        methods=["manual"], manual_urls=[art(101), art(102)],
        source_resolver=resolver)

    assert result["new"] == 2
    doc = await doc_dao.get_by_url(db, art(101))
    assert doc.source_id == owner.id      # attributed to the tier-1 owner


async def test_run_backfill_honors_cancel(container, db):
    fetcher = FakeFetcher(_site_pages())
    container.pipeline.fetcher = fetcher
    source = await _web_news_source(db)

    async def always_cancel():
        return True

    result = await bf.run_backfill(
        db, source=source, pipeline=container.pipeline, fetcher=fetcher,
        methods=["sitemap"], start="2024-01-01", end="2024-03-01",
        should_cancel=always_cancel)
    assert result["cancelled"] is True
    assert result["new"] == 0


# ---------------------------------------------------------------------------
# API endpoint contract
# ---------------------------------------------------------------------------

def _make_web_news(client) -> int:
    resp = client.post("/api/sources", json={
        "name": "BackfillTarget", "type": "web_news",
        "config": {"index_url": f"{BASE}/economy", "link_pattern": PATTERN},
        "credibility_tier": 2})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _offline(client):
    """Point the app container's fetchers at a fake so an embedded backfill
    job (if it runs) never touches the network."""
    fake = FakeFetcher(_site_pages())
    client.app.state.container.fetcher = fake
    client.app.state.container.pipeline.fetcher = fake


def test_backfill_endpoint_enqueues(client):
    _offline(client)
    sid = _make_web_news(client)
    resp = client.post(f"/api/sources/{sid}/backfill",
                       json={"methods": ["sitemap"], "limit": 10})
    assert resp.status_code == 202, resp.text
    assert isinstance(resp.json()["job_id"], int)


def test_backfill_endpoint_validation(client):
    sid = _make_web_news(client)
    assert client.post(f"/api/sources/{sid}/backfill",
                       json={"methods": ["telepathy"]}).status_code == 422
    assert client.post(f"/api/sources/{sid}/backfill",
                       json={"start_date": "01-2024"}).status_code == 422
    assert client.post("/api/sources/999999/backfill",
                       json={}).status_code == 404


def test_backfill_rejects_source_without_domain_or_manual(client):
    sid = client.post("/api/sources", json={
        "name": "ManualBucket", "type": "manual", "config": {},
        "credibility_tier": 4}).json()["id"]
    assert client.post(f"/api/sources/{sid}/backfill",
                       json={}).status_code == 400
    _offline(client)
    ok = client.post(f"/api/sources/{sid}/backfill",
                     json={"methods": ["manual"], "manual_urls": [art(101)]})
    assert ok.status_code == 202, ok.text
