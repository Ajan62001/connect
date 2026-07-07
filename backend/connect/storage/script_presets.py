"""Script-preset DAO — custom script-type presets (v27).

Built-ins live in ``connect/content/presets.py`` (code); this stores the
user-authored ones. Ids are exposed to the rest of the app as ``custom:<n>``
(the ``ScriptPreset.id`` string) so a campaign's ``script_type`` pick is one
namespaced string whether it points at a builtin slug or a custom row.
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.content.presets import ScriptPreset
from connect.storage.pg import Jsonb, utc_now

CUSTOM_PREFIX = "custom:"

_COLS = ("id, owner_id, name, guidance, formats, scene_count, caption_style,"
         " visual_style, created_at, updated_at")

_PATCHABLE = ("name", "guidance", "scene_count", "caption_style",
              "visual_style")


def _to_preset(row: dict[str, Any]) -> ScriptPreset:
    return ScriptPreset(
        id=f"{CUSTOM_PREFIX}{row['id']}", name=row["name"],
        guidance=row["guidance"], formats=list(row["formats"] or []),
        scene_count=row["scene_count"], caption_style=row["caption_style"],
        visual_style=row["visual_style"], builtin=False)


async def insert(conn: psycopg.AsyncConnection, *, owner_id: int,
                 data: dict[str, Any]) -> ScriptPreset:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO script_preset (owner_id, name, guidance, formats,"
            " scene_count, caption_style, visual_style, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
            f" RETURNING {_COLS}",
            (owner_id, data["name"], data.get("guidance", ""),
             Jsonb(list(data.get("formats") or [])), data.get("scene_count"),
             data.get("caption_style"), data.get("visual_style"), utc_now()))
        return _to_preset(await cur.fetchone())


async def get(conn: psycopg.AsyncConnection, preset_id: int) -> ScriptPreset | None:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM script_preset WHERE id = %s", (preset_id,))
    row = await cur.fetchone()
    return _to_preset(row) if row else None


async def list_all(conn: psycopg.AsyncConnection) -> list[ScriptPreset]:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM script_preset ORDER BY name, id")
    return [_to_preset(r) for r in await cur.fetchall()]


async def owner_of(conn: psycopg.AsyncConnection, preset_id: int) -> int | None:
    cur = await conn.execute(
        "SELECT owner_id FROM script_preset WHERE id = %s", (preset_id,))
    row = await cur.fetchone()
    return int(row["owner_id"]) if row else None


async def update(conn: psycopg.AsyncConnection, preset_id: int, *,
                 patch: dict[str, Any]) -> ScriptPreset | None:
    sets, params = [], []
    for col in _PATCHABLE:
        if col in patch and patch[col] is not None:
            sets.append(f"{col} = %s")
            params.append(patch[col])
    if "formats" in patch and patch["formats"] is not None:
        sets.append("formats = %s")
        params.append(Jsonb(list(patch["formats"])))
    if not sets:
        return await get(conn, preset_id)
    sets.append("updated_at = %s")
    params.append(utc_now())
    params.append(preset_id)
    async with conn.transaction():
        cur = await conn.execute(
            f"UPDATE script_preset SET {', '.join(sets)} WHERE id = %s"
            f" RETURNING {_COLS}", tuple(params))
        row = await cur.fetchone()
    return _to_preset(row) if row else None


async def delete(conn: psycopg.AsyncConnection, preset_id: int) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM script_preset WHERE id = %s", (preset_id,))
    return cur.rowcount > 0
