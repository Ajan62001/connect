"""Global channel defaults — voice / character / script-type (v27).

The floor under the per-workspace ``workspace_channel`` binding, exactly like
``social/settings.py`` is the global floor under a workspace's ``post_settings``.
Stored as a JSON blob under the ``channel_settings`` app_setting key; absent, the
code defaults (all None) apply. This is intentionally SEPARATE from PostSettings:
voice ids do not belong on the card-style model, whose ``_merge`` also drops
unknown keys.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from connect.domain.models import ChannelSettings
from connect.storage import app_settings as app_settings_dao

CHANNEL_SETTINGS_KEY = "channel_settings"
DEFAULTS = ChannelSettings()


def _merge(base: ChannelSettings,
           overrides: dict[str, Any] | None) -> ChannelSettings:
    if not overrides:
        return base
    clean = {k: v for k, v in overrides.items()
             if v is not None and k in ChannelSettings.model_fields}
    if not clean:
        return base
    return ChannelSettings(**{**base.model_dump(), **clean})


async def get_global(conn: psycopg.AsyncConnection) -> ChannelSettings:
    raw = await app_settings_dao.get(conn, CHANNEL_SETTINGS_KEY)
    if not raw:
        return DEFAULTS
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return DEFAULTS
    return _merge(DEFAULTS, data if isinstance(data, dict) else None)


async def set_global(conn: psycopg.AsyncConnection,
                     settings: ChannelSettings) -> None:
    await app_settings_dao.set_value(conn, CHANNEL_SETTINGS_KEY,
                                     settings.model_dump_json())
