"""Telegram adapter: t.me/s HTML fixture -> items/documents, line breaks,
since cursor, link rows — canned fetcher, zero network."""

from __future__ import annotations

from pathlib import Path

from canned_web import FakeFetcher, html_page
from connect.ingestion.poller import SourcePoller
from connect.sources.adapters.telegram import TelegramAdapter, parse_channel_page
from connect.storage import documents as doc_dao
from connect.storage import links as link_dao
from connect.storage import sources as source_dao

FIXTURE = Path(__file__).parent / "fixtures" / "telegram_channel.html"
CHANNEL = "PIB_FactCheck"
PREVIEW_URL = f"https://t.me/s/{CHANNEL}"
PIB_CLARIFICATION_URL = "https://pib.gov.in/PressReleasePage.aspx?PRID=2026101"


def test_parse_channel_page_fixture():
    items = parse_channel_page(FIXTURE.read_bytes(), CHANNEL)

    # 3 blocks on the page; the photo-only post (4502) has no text -> skipped
    assert len(items) == 2
    # newest first (the page lists oldest -> newest)
    assert [i.url for i in items] == [
        f"https://t.me/{CHANNEL}/4503",
        f"https://t.me/{CHANNEL}/4501",
    ]

    roundup, factcheck = items
    assert roundup.title.startswith(
        f"{CHANNEL}: PIB Fact Check Weekly Roundup")
    assert roundup.published_at == "2026-06-10T12:45:00Z"
    assert roundup.author == CHANNEL
    assert roundup.media_type == "telegram"
    assert roundup.link_urls == (f"https://t.me/{CHANNEL}",)

    assert factcheck.published_at == "2026-06-10T08:00:00Z"
    assert factcheck.content_text.startswith("A viral message circulating")
    # <br><br> became a paragraph break, bold text flattened to plain text
    assert "Rs 2,000.\n\nThis claim is FAKE." in factcheck.content_text
    assert factcheck.link_urls == (PIB_CLARIFICATION_URL,)
    # the raw blob is the message block's own HTML
    assert b"tgme_widget_message" in factcheck.raw
    assert b'data-post="PIB_FactCheck/4501"' in factcheck.raw


def test_parse_channel_page_garbage_yields_empty():
    assert parse_channel_page(b"\x00\x01 not html", CHANNEL) == []


async def test_discover_filters_by_since_cursor():
    fetcher = FakeFetcher({PREVIEW_URL: ("text/html", FIXTURE.read_bytes())})
    adapter = TelegramAdapter(fetcher)

    items = await adapter.discover({"channel": CHANNEL})
    assert len(items) == 2

    items = await adapter.discover(
        {"channel": CHANNEL}, since="2026-06-10T10:00:00Z")
    assert [i.url for i in items] == [f"https://t.me/{CHANNEL}/4503"]

    # the preview endpoint is the registered source -> robots-exempt fetch
    assert fetcher.calls == [PREVIEW_URL, PREVIEW_URL]


async def test_poll_telegram_source_end_to_end(container, db):
    pages = {
        PREVIEW_URL: ("text/html", FIXTURE.read_bytes()),
        PIB_CLARIFICATION_URL: html_page(
            "UPI charges clarification",
            "<p>The Government clarified that no charges apply to normal "
            "UPI transactions and the viral message is fabricated.</p>"),
    }
    fetcher = FakeFetcher(pages)
    container.pipeline.fetcher = fetcher
    adapter = TelegramAdapter(fetcher)

    source = await source_dao.insert(
        db, name="PIB Fact Check tg", type_="telegram",
        config={"channel": CHANNEL}, credibility_tier=1)
    poller = SourcePoller(container.pool, pipeline=container.pipeline,
                          adapters={"telegram": adapter})

    status = await poller.poll_source(db, source)
    assert status == "ok: 2 new, 0 dup, 0 error"

    doc = await doc_dao.get_by_url(db, f"https://t.me/{CHANNEL}/4501")
    assert doc is not None
    assert doc.media_type == "telegram"
    assert doc.author == CHANNEL
    assert doc.source_id == source.id
    assert doc.published_at == "2026-06-10T08:00:00.000Z"
    assert doc.title.startswith(
        f"{CHANNEL}: A viral message circulating on WhatsApp")
    assert len(doc.title) == len(f"{CHANNEL}: ") + 80  # first 80 chars

    # the in-post official link became a document_link row and was followed
    links = await link_dao.list_for_document(db, doc.id)
    assert [l.url for l in links] == [PIB_CLARIFICATION_URL]
    assert links[0].is_official
    assert links[0].status == "fetched"

    # the roundup post's t.me self-link is stored but never auto-followed
    roundup = await doc_dao.get_by_url(
        db, f"https://t.me/{CHANNEL}/4503")
    assert roundup is not None
    tg_links = await link_dao.list_for_document(db, roundup.id)
    assert [l.url for l in tg_links] == [f"https://t.me/{CHANNEL}"]
    assert tg_links[0].status == "not_followed"

    # second poll: cursor (last_polled_at) filters everything out
    polled = await source_dao.get(db, source.id)
    status = await poller.poll_source(db, polled)
    assert status == "ok: 0 new, 0 dup, 0 error"
