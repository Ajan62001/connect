"""Stub adapters for types whose configs validate but whose fetch logic
lands in later phases (scrape: PRS; api: Indian Kanoon; search: Tavily)."""

from __future__ import annotations

from typing import Any

from connect.domain.models import DiscoveredItem
from connect.sources.base import SourceConfig, parse_source_config


class _StubAdapter:
    type_name = "stub"

    def __init__(self, fetcher: Any = None):  # uniform constructor signature
        self.fetcher = fetcher

    def validate(self, config: dict[str, Any]) -> SourceConfig:
        return parse_source_config(self.type_name, config)

    async def discover(self, config: dict[str, Any],
                       since: str | None = None) -> list[DiscoveredItem]:
        raise NotImplementedError(
            f"{self.type_name!r} sources are not pollable yet (later phase)")

    async def sample(self, config: dict[str, Any],
                     limit: int = 5) -> list[DiscoveredItem]:
        raise NotImplementedError(
            f"{self.type_name!r} sources cannot be tested yet (later phase)")


class ScrapeAdapter(_StubAdapter):
    type_name = "scrape"


class ApiAdapter(_StubAdapter):
    type_name = "api"


class SearchAdapter(_StubAdapter):
    type_name = "search"
