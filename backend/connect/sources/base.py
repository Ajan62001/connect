"""SourceAdapter Protocol + per-type config models (discriminated union).

A source row's `type` selects an adapter class from the import-time registry;
its `config` JSON is validated against the matching Pydantic model at
create/update time — misconfigured sources fail at registration, not
silently at poll time.

Phase 0 ships a working `rss` adapter; scrape/api/search configs VALIDATE
(so rows can be registered ahead of time) but their adapters raise
NotImplementedError. `manual` needs no adapter.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from connect.domain.models import DiscoveredItem


class SourceConfigError(ValueError):
    """Config JSON does not validate against the source type's model."""


class AdapterSkip(Exception):
    """A poll/test must cleanly no-op (e.g. a provider key is not
    configured). The poller records ``skipped: <reason>`` as the poll
    status; POST /sources/test returns ok=false with the reason. Never an
    error, never retried."""


class _FrozenConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")


class RssConfig(_FrozenConfig):
    type: Literal["rss"] = "rss"
    feed_url: str
    poll_interval_minutes: int = Field(default=30, ge=1)

    @field_validator("feed_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("feed_url must be an http(s) URL")
        return v


class ScrapeConfig(_FrozenConfig):  # stub — adapter lands with PRS in Phase 4
    type: Literal["scrape"] = "scrape"
    index_urls: list[str] = Field(default_factory=list)
    link_patterns: list[str] = Field(default_factory=list)
    poll_interval_minutes: int = Field(default=120, ge=1)


class ApiConfig(_FrozenConfig):  # stub — Indian Kanoon etc.
    type: Literal["api"] = "api"
    base_url: str | None = None
    auth_header_env: str | None = None


class SearchConfig(_FrozenConfig):  # stub — Tavily/SearXNG in Phase 3
    type: Literal["search"] = "search"
    provider: Literal["tavily", "brave", "serper", "searxng"] = "tavily"
    api_key_env: str | None = None
    default_params: dict[str, Any] = Field(default_factory=dict)


class ManualConfig(_FrozenConfig):
    type: Literal["manual"] = "manual"


# X handle: 1-15 word characters, no '@' (the UI strips it; the API rejects).
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# Telegram public username: letter first, then letters/digits/underscore,
# 5-32 chars total (t.me rules).
_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class TwitterConfig(_FrozenConfig):
    """twitterapi.io advanced-search source: all handles are OR-batched into
    ONE search call per poll cycle (cost discipline — never per-handle
    timeline calls)."""
    type: Literal["twitter"] = "twitter"
    handles: list[str] = Field(min_length=1, max_length=50)
    poll_interval_minutes: int = Field(default=30, ge=1)

    @field_validator("handles")
    @classmethod
    def _valid_handles(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for handle in v:
            handle = handle.strip()
            if handle.startswith("@"):
                raise ValueError(f"handle {handle!r} must not include '@'")
            if not _HANDLE_RE.match(handle):
                raise ValueError(
                    f"invalid X handle {handle!r} "
                    "(1-15 letters/digits/underscore)")
            cleaned.append(handle)
        return cleaned


class TelegramConfig(_FrozenConfig):
    """Public-channel preview source (https://t.me/s/<channel>) — free, no
    key, works only for channels with public previews enabled."""
    type: Literal["telegram"] = "telegram"
    channel: str
    poll_interval_minutes: int = Field(default=30, ge=1)

    @field_validator("channel")
    @classmethod
    def _valid_channel(cls, v: str) -> str:
        v = v.strip().lstrip("@")
        if not _CHANNEL_RE.match(v):
            raise ValueError(
                f"invalid Telegram channel {v!r} "
                "(public username, e.g. PIB_FactCheck)")
        return v


SourceConfig = Annotated[
    Union[RssConfig, ScrapeConfig, ApiConfig, SearchConfig, ManualConfig,
          TwitterConfig, TelegramConfig],
    Field(discriminator="type"),
]


class _ConfigEnvelope(BaseModel):
    config: SourceConfig


def parse_source_config(type_: str, config: dict[str, Any]) -> SourceConfig:
    """Validate a config dict against its type's model. Raises
    SourceConfigError with a readable message on failure."""
    payload = dict(config or {})
    declared = payload.get("type")
    if declared is not None and declared != type_:
        raise SourceConfigError(
            f"config.type {declared!r} does not match source type {type_!r}")
    payload["type"] = type_
    try:
        return _ConfigEnvelope(config=payload).config
    except Exception as e:  # pydantic.ValidationError
        raise SourceConfigError(str(e)) from e


class SourceAdapter(Protocol):
    """The user-extensibility seam. Implementations are registered in
    registry.SOURCE_ADAPTERS keyed by source type."""

    type_name: str

    def validate(self, config: dict[str, Any]) -> SourceConfig:
        """Parse + validate the config dict (raises SourceConfigError)."""
        ...

    async def discover(self, config: dict[str, Any],
                       since: str | None = None) -> list[DiscoveredItem]:
        """Items currently available from the source (newest first)."""
        ...

    async def sample(self, config: dict[str, Any],
                     limit: int = 5) -> list[DiscoveredItem]:
        """Dry-run for POST /sources/test."""
        ...
