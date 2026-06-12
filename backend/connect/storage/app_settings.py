"""app_setting DAO — admin-editable deployment globals (budgets).

Precedence rule (tenancy design §4/§9): a per-user override column on
``app_user`` trumps ``app_setting``, which trumps the env ``Settings``
default. **app_setting wins when present; the admin UI is the only
writer** — except for first-boot seeding here, which is
``ON CONFLICT DO NOTHING`` so a later admin edit is never clobbered by a
container restart (duplicate seeding across api replicas is harmless for
the same reason — runtime design risk #6).

Seeded keys (locked user decisions):

- ``member_daily_budget_usd``               $0.50/day general (members)
- ``member_investigation_daily_budget_usd`` $2.00/day investigation (members)
- ``global_daily_budget_usd``               $10/day deployment-wide backstop
- ``daily_llm_budget_usd``                  $2/day general purpose envelope
- ``investigation_daily_budget_usd``        $10/day investigation envelope

Admins keep the env defaults ($2 general / $10 investigation) — their
per-user ceiling is the purpose envelope itself. The TenantGovernor
(llm/spend.py) reads these per check, so an admin PATCH takes effect in
every process without a restart.
"""

from __future__ import annotations

import psycopg

from connect.orchestration.config import Settings

# every admin-editable budget key -> the Settings attribute that seeds it
# (and remains the fallback when the row is absent)
BUDGET_KEYS: dict[str, str] = {
    "member_daily_budget_usd": "member_daily_budget_usd",
    "member_investigation_daily_budget_usd":
        "member_investigation_daily_budget_usd",
    "global_daily_budget_usd": "global_daily_budget_usd",
    "daily_llm_budget_usd": "daily_llm_budget_usd",
    "investigation_daily_budget_usd": "investigation_daily_budget_usd",
}


async def get(conn: psycopg.AsyncConnection, key: str) -> str | None:
    cur = await conn.execute(
        "SELECT value FROM app_setting WHERE key = %s", (key,))
    row = await cur.fetchone()
    return row["value"] if row is not None else None


async def get_many(conn: psycopg.AsyncConnection,
                   keys: tuple[str, ...] | list[str]) -> dict[str, str]:
    cur = await conn.execute(
        "SELECT key, value FROM app_setting WHERE key = ANY(%s)",
        (list(keys),))
    return {row["key"]: row["value"] for row in await cur.fetchall()}


async def set_value(conn: psycopg.AsyncConnection, key: str,
                    value: str) -> None:
    """Upsert one setting — the admin PATCH path (the only writer besides
    first-boot seeding)."""
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO app_setting (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value))


async def effective_budgets(conn: psycopg.AsyncConnection,
                            settings: Settings) -> dict[str, float]:
    """Every budget key's effective value: app_setting when present and
    numeric, else the env Settings default (design §9 precedence)."""
    rows = await get_many(conn, list(BUDGET_KEYS))
    out: dict[str, float] = {}
    for key, attr in BUDGET_KEYS.items():
        value = rows.get(key)
        try:
            out[key] = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            out[key] = float(getattr(settings, attr))
    return out


async def seed_defaults(conn: psycopg.AsyncConnection,
                        settings: Settings) -> int:
    """Insert the budget seeds where absent; returns rows actually added."""
    added = 0
    for key, attr in BUDGET_KEYS.items():
        cur = await conn.execute(
            "INSERT INTO app_setting (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO NOTHING",
            (key, str(getattr(settings, attr))))
        added += cur.rowcount
    return added
