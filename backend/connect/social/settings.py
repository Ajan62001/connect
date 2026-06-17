"""Post-generation settings — a global default (app_setting) overridden
per-workspace.

The global default lives under the ``post_settings`` app_setting key as a JSON
blob; absent, the code defaults apply. A workspace stores a partial overrides
dict; ``effective`` merges (code defaults < global < workspace) so a caption/
card is always generated from a complete, resolved PostSettings.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from connect.domain.models import PostSettings
from connect.storage import app_settings as app_settings_dao

POST_SETTINGS_KEY = "post_settings"
DEFAULTS = PostSettings()


def _merge(base: PostSettings, overrides: dict[str, Any] | None) -> PostSettings:
    if not overrides:
        return base
    clean = {k: v for k, v in overrides.items()
             if v is not None and k in PostSettings.model_fields}
    if not clean:
        return base
    return PostSettings(**{**base.model_dump(), **clean})


async def get_global(conn: psycopg.AsyncConnection) -> PostSettings:
    raw = await app_settings_dao.get(conn, POST_SETTINGS_KEY)
    if not raw:
        return DEFAULTS
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return DEFAULTS
    return _merge(DEFAULTS, data if isinstance(data, dict) else None)


async def set_global(conn: psycopg.AsyncConnection,
                     settings: PostSettings) -> None:
    await app_settings_dao.set_value(conn, POST_SETTINGS_KEY,
                                     settings.model_dump_json())


async def effective(conn: psycopg.AsyncConnection,
                    workspace: Any = None) -> PostSettings:
    """The resolved settings to generate a post with: global, then the
    workspace's overrides on top."""
    base = await get_global(conn)
    overrides = getattr(workspace, "post_settings", None) if workspace else None
    return _merge(base, overrides)


def load_logo(store: Any, settings: PostSettings) -> bytes | None:
    """Read the brand-logo bytes for ``settings.logo_sha`` from the social-logo
    BlobStore; None when unset, absent, or unreadable (the card just renders
    without a logo). Used by every render path so the look stays consistent."""
    sha = settings.logo_sha
    if not sha or store is None:
        return None
    rel = f"{sha[:2]}/{sha}"
    try:
        return store.get(rel) if store.exists(rel) else None
    except OSError:
        return None
