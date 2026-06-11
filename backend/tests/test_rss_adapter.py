"""RSS adapter parses a fixture feed file — zero network."""

from __future__ import annotations

from pathlib import Path

from connect.sources.adapters.rss import parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"


def test_parse_feed_fixture():
    items = parse_feed(FIXTURE.read_bytes())

    # the entry without a link is skipped
    assert len(items) == 2

    first = items[0]
    assert first.title == "RBI announces expansion of digital rupee pilot"
    assert first.url == (
        "https://rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid=10001")
    assert first.published_at is not None
    assert first.published_at.startswith("2026-06-10T")
    assert first.summary == "The pilot now covers twelve more cities."


def test_parse_feed_garbage_yields_empty():
    assert parse_feed(b"this is not xml at all") == []
