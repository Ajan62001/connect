"""Discriminated-union source-config validation."""

from __future__ import annotations

import pytest

from connect.sources.base import (
    RssConfig,
    SourceConfigError,
    TelegramConfig,
    TwitterConfig,
    parse_source_config,
)
from connect.sources.registry import SOURCE_ADAPTERS


def test_valid_rss_config_with_defaults():
    cfg = parse_source_config("rss", {"feed_url": "https://rbi.org.in/x.xml"})
    assert isinstance(cfg, RssConfig)
    assert cfg.poll_interval_minutes == 30


def test_rss_missing_feed_url_rejected():
    with pytest.raises(SourceConfigError):
        parse_source_config("rss", {})


def test_rss_non_http_url_rejected():
    with pytest.raises(SourceConfigError):
        parse_source_config("rss", {"feed_url": "ftp://example.com/feed"})


def test_type_mismatch_rejected():
    with pytest.raises(SourceConfigError):
        parse_source_config("rss", {"type": "scrape",
                                    "feed_url": "https://x.example/f"})


def test_unknown_type_rejected():
    with pytest.raises(SourceConfigError):
        parse_source_config("carrier_pigeon", {})


def test_stub_types_validate_but_adapters_refuse():
    for type_ in ("scrape", "api", "search"):
        cfg = parse_source_config(type_, {})
        assert cfg.type == type_


@pytest.mark.asyncio
async def test_stub_adapter_discover_raises():
    adapter = SOURCE_ADAPTERS["scrape"]()
    with pytest.raises(NotImplementedError):
        await adapter.discover({})


def test_manual_config_validates():
    cfg = parse_source_config("manual", {})
    assert cfg.type == "manual"


def test_registry_covers_all_adapter_types():
    assert set(SOURCE_ADAPTERS) == {
        "rss", "twitter", "telegram", "scrape", "api", "search"}


# --- twitter config ----------------------------------------------------------


def test_twitter_config_valid_with_defaults():
    cfg = parse_source_config("twitter", {"handles": ["PIB_India", "RBI"]})
    assert isinstance(cfg, TwitterConfig)
    assert cfg.handles == ["PIB_India", "RBI"]
    assert cfg.poll_interval_minutes == 30


def test_twitter_handles_strip_whitespace():
    cfg = parse_source_config("twitter", {"handles": [" PMOIndia "]})
    assert cfg.handles == ["PMOIndia"]


@pytest.mark.parametrize("handles", [
    [],                          # empty
    ["@PIB_India"],              # leading @
    ["has space"],               # whitespace inside
    ["way_too_long_for_x_15"],   # > 15 chars
    ["ok", ""],                  # empty entry
    ["dot.handle"],              # bad char
    ["h"] * 51,                  # > 50 handles
])
def test_twitter_bad_handles_rejected(handles):
    with pytest.raises(SourceConfigError):
        parse_source_config("twitter", {"handles": handles})


# --- telegram config ---------------------------------------------------------


def test_telegram_config_valid_and_at_stripped():
    cfg = parse_source_config("telegram", {"channel": "PIB_FactCheck"})
    assert isinstance(cfg, TelegramConfig)
    assert cfg.channel == "PIB_FactCheck"
    assert cfg.poll_interval_minutes == 30
    # a pasted '@channel' is normalized, not rejected
    assert parse_source_config(
        "telegram", {"channel": "@MIB_India"}).channel == "MIB_India"


@pytest.mark.parametrize("channel", [
    "",                       # empty
    "abc",                    # too short (< 5 chars)
    "1starts_with_digit",
    "has space",
    "https://t.me/s/PIB_FactCheck",  # paste the username, not the URL
    "x" * 33,                 # too long
])
def test_telegram_bad_channel_rejected(channel):
    with pytest.raises(SourceConfigError):
        parse_source_config("telegram", {"channel": channel})


def test_telegram_missing_channel_rejected():
    with pytest.raises(SourceConfigError):
        parse_source_config("telegram", {})
