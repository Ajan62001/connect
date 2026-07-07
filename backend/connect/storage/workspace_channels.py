"""Workspace-channel DAO — a workspace's publishing channel binding (v27).

1:1 with ``workspace``. Rows are lazily created on first PUT (upsert). The
``zapier_webhook_url`` and ``credentials`` are secrets — the router masks them
for non-owner viewers; the DAO returns them and lets the caller decide.
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.domain.models import WorkspaceChannel
from connect.storage.pg import Jsonb, utc_now

_COLS = ("workspace_id, platform, channel_name, channel_handle, external_id,"
         " zapier_webhook_url, default_voice_id, default_character_id,"
         " default_script_type, credentials, created_at, updated_at")

_PATCHABLE = ("platform", "channel_name", "channel_handle", "external_id",
              "zapier_webhook_url", "default_voice_id", "default_character_id",
              "default_script_type")


def _to_model(row: dict[str, Any], *, reveal_secrets: bool) -> WorkspaceChannel:
    webhook = row["zapier_webhook_url"]
    creds = row["credentials"] or {}
    return WorkspaceChannel(
        workspace_id=row["workspace_id"], platform=row["platform"],
        channel_name=row["channel_name"], channel_handle=row["channel_handle"],
        external_id=row["external_id"],
        zapier_webhook_url=webhook if reveal_secrets else None,
        webhook_set=bool(webhook),
        default_voice_id=row["default_voice_id"],
        default_character_id=row["default_character_id"],
        default_script_type=row["default_script_type"],
        has_credentials=bool(creds),
        created_at=str(row["created_at"]) if row["created_at"] else None,
        updated_at=str(row["updated_at"]) if row["updated_at"] else None)


async def get(conn: psycopg.AsyncConnection, workspace_id: int, *,
              reveal_secrets: bool = False) -> WorkspaceChannel | None:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM workspace_channel WHERE workspace_id = %s",
        (workspace_id,))
    row = await cur.fetchone()
    return _to_model(row, reveal_secrets=reveal_secrets) if row else None


async def get_raw(conn: psycopg.AsyncConnection,
                  workspace_id: int) -> dict[str, Any] | None:
    """The raw row (secrets included) — for the render/publish resolvers that
    need the webhook + voice/character defaults, never serialized to a client."""
    cur = await conn.execute(
        f"SELECT {_COLS} FROM workspace_channel WHERE workspace_id = %s",
        (workspace_id,))
    return await cur.fetchone()


async def for_campaign(conn: psycopg.AsyncConnection,
                       campaign_id: int) -> dict[str, Any] | None:
    """The channel row for a campaign's workspace (via campaign.workspace_id),
    or None. Raw (secrets included) — used by the publish router."""
    cur = await conn.execute(
        f"SELECT wc.workspace_id, wc.platform, wc.channel_name,"
        " wc.channel_handle, wc.external_id, wc.zapier_webhook_url,"
        " wc.default_voice_id, wc.default_character_id, wc.default_script_type,"
        " wc.credentials, wc.created_at, wc.updated_at"
        " FROM workspace_channel wc"
        " JOIN campaign c ON c.workspace_id = wc.workspace_id"
        " WHERE c.id = %s", (campaign_id,))
    return await cur.fetchone()


async def upsert(conn: psycopg.AsyncConnection, workspace_id: int, *,
                 patch: dict[str, Any]) -> WorkspaceChannel:
    """Create-or-update the channel row. Only the keys present in ``patch``
    (non-None) are written; on insert the rest take their column defaults."""
    fields = {k: patch[k] for k in _PATCHABLE
              if k in patch and patch[k] is not None}
    async with conn.transaction():
        # ensure a row exists (defaults), then apply the patch — keeps the
        # INSERT column list stable regardless of which keys the caller sent
        await conn.execute(
            "INSERT INTO workspace_channel (workspace_id, created_at)"
            " VALUES (%s, %s) ON CONFLICT (workspace_id) DO NOTHING",
            (workspace_id, utc_now()))
        if fields:
            sets = ", ".join(f"{k} = %s" for k in fields)
            params = [*fields.values(), utc_now(), workspace_id]
            await conn.execute(
                f"UPDATE workspace_channel SET {sets}, updated_at = %s"
                " WHERE workspace_id = %s", tuple(params))
        cur = await conn.execute(
            f"SELECT {_COLS} FROM workspace_channel WHERE workspace_id = %s",
            (workspace_id,))
        return _to_model(await cur.fetchone(), reveal_secrets=True)
