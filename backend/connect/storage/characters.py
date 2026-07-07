"""Character DAO — the shared roster of presenter personas (v27).

Shared across all users (any authed user lists/creates); edit/delete is
owner-or-admin (enforced at the router). Deleting a character is safe — every
reference (workspace_channel.default_character_id) is ON DELETE SET NULL.
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.domain.models import Character
from connect.storage.pg import Jsonb, utc_now

_COLS = ("id, owner_id, name, description, speaking_style, sample_line,"
         " voice_id, heygen_avatar_id, catchphrases, sign_off, avatar_sha,"
         " created_at, updated_at")

_PATCHABLE = ("name", "description", "speaking_style", "sample_line",
              "voice_id", "heygen_avatar_id", "sign_off", "avatar_sha")


def _to_model(row: dict[str, Any]) -> Character:
    return Character(
        id=row["id"], name=row["name"], description=row["description"],
        speaking_style=row["speaking_style"], sample_line=row["sample_line"],
        voice_id=row["voice_id"], heygen_avatar_id=row["heygen_avatar_id"],
        catchphrases=list(row["catchphrases"] or []),
        sign_off=row["sign_off"], avatar_sha=row["avatar_sha"],
        owner_id=row["owner_id"], created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]) if row["updated_at"] else None)


async def insert(conn: psycopg.AsyncConnection, *, owner_id: int,
                 data: dict[str, Any]) -> Character:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO character (owner_id, name, description,"
            " speaking_style, sample_line, voice_id, heygen_avatar_id,"
            " catchphrases, sign_off, avatar_sha, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            f" RETURNING {_COLS}",
            (owner_id, data["name"], data.get("description", ""),
             data.get("speaking_style", ""), data.get("sample_line", ""),
             data.get("voice_id"), data.get("heygen_avatar_id"),
             Jsonb(list(data.get("catchphrases") or [])),
             data.get("sign_off", ""), data.get("avatar_sha"), utc_now()))
        return _to_model(await cur.fetchone())


async def get(conn: psycopg.AsyncConnection, character_id: int) -> Character | None:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM character WHERE id = %s", (character_id,))
    row = await cur.fetchone()
    return _to_model(row) if row else None


async def list_all(conn: psycopg.AsyncConnection) -> list[Character]:
    cur = await conn.execute(
        f"SELECT {_COLS} FROM character ORDER BY name, id")
    return [_to_model(r) for r in await cur.fetchall()]


async def owner_of(conn: psycopg.AsyncConnection, character_id: int) -> int | None:
    cur = await conn.execute(
        "SELECT owner_id FROM character WHERE id = %s", (character_id,))
    row = await cur.fetchone()
    return int(row["owner_id"]) if row else None


async def update(conn: psycopg.AsyncConnection, character_id: int, *,
                 patch: dict[str, Any]) -> Character | None:
    sets, params = [], []
    for col in _PATCHABLE:
        if col in patch and patch[col] is not None:
            sets.append(f"{col} = %s")
            params.append(patch[col])
    if "catchphrases" in patch and patch["catchphrases"] is not None:
        sets.append("catchphrases = %s")
        params.append(Jsonb(list(patch["catchphrases"])))
    if not sets:
        return await get(conn, character_id)
    sets.append("updated_at = %s")
    params.append(utc_now())
    params.append(character_id)
    async with conn.transaction():
        cur = await conn.execute(
            f"UPDATE character SET {', '.join(sets)} WHERE id = %s"
            f" RETURNING {_COLS}", tuple(params))
        row = await cur.fetchone()
    return _to_model(row) if row else None


async def delete(conn: psycopg.AsyncConnection, character_id: int) -> bool:
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM character WHERE id = %s", (character_id,))
    return cur.rowcount > 0
