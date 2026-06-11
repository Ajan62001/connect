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

Admins keep the env defaults ($2 general / $10 investigation). The
TenantGovernor that reads these lands with the auth workstream; the rows
ship now so budgets are admin-adjustable data from the first boot, not a
schema afterthought.
"""

from __future__ import annotations

import psycopg

from connect.orchestration.config import Settings


async def get(conn: psycopg.AsyncConnection, key: str) -> str | None:
    cur = await conn.execute(
        "SELECT value FROM app_setting WHERE key = %s", (key,))
    row = await cur.fetchone()
    return row["value"] if row is not None else None


async def seed_defaults(conn: psycopg.AsyncConnection,
                        settings: Settings) -> int:
    """Insert the budget seeds where absent; returns rows actually added."""
    seeds = (
        ("member_daily_budget_usd",
         settings.member_daily_budget_usd),
        ("member_investigation_daily_budget_usd",
         settings.member_investigation_daily_budget_usd),
        ("global_daily_budget_usd",
         settings.global_daily_budget_usd),
    )
    added = 0
    for key, value in seeds:
        cur = await conn.execute(
            "INSERT INTO app_setting (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO NOTHING",
            (key, str(value)))
        added += cur.rowcount
    return added
