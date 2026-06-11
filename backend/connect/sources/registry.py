"""Import-time adapter registry (log_buster PARSER_REGISTRY pattern).

Resolution: source row.type -> adapter CLASS; the container instantiates
with its fetcher (twitter additionally gets the twitterapi.io key — wired
in the composition root). Adding a source type is one line here plus an
adapter module. `manual` deliberately has no adapter.
"""

from __future__ import annotations

from connect.sources.adapters.rss import RssAdapter
from connect.sources.adapters.stubs import ApiAdapter, ScrapeAdapter, SearchAdapter
from connect.sources.adapters.telegram import TelegramAdapter
from connect.sources.adapters.twitter import TwitterAdapter

SOURCE_ADAPTERS: dict[str, type] = {
    "rss": RssAdapter,
    "twitter": TwitterAdapter,
    "telegram": TelegramAdapter,
    "scrape": ScrapeAdapter,
    "api": ApiAdapter,
    "search": SearchAdapter,
}

# Types the poller actually polls — adapters with working discovery. The
# stubs raise NotImplementedError until their phases land; `manual` has no
# adapter at all.
POLLABLE_TYPES: tuple[str, ...] = ("rss", "twitter", "telegram")
