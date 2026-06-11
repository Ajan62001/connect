"""The spend ledger + budget governor — cost control is structural.

Every LLM response's usage is recorded as an llm_call row with a computed
cost; the Governor compares today's ledger + a projection against the daily
budget BEFORE work is queued, so an over-budget day degrades to T0-only
(documents stay 'pending', never lost).

PRICING is the single editable home for per-model USD/MTok rates (verified
against the current Anthropic catalog via the claude-api skill, 2026-06):
haiku-4-5 1/5, sonnet-4-6 3/15, opus-4-8 5/25; Message Batches are 50% off;
cache reads bill at ~0.1x the input rate.

v0.2: the Governor holds the POOL (design §2 — one connection per check,
never a held connection), so ``check()``/``spent_today()`` are async.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.llm.provider import Usage
from connect.storage.pg import utc_now

# model-id prefix -> (USD per MTok input, USD per MTok output).
# Longest-prefix match so date-suffixed ids ('claude-haiku-4-5-20251001')
# still resolve. EDIT HERE when prices or models change.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}

# Unknown model: assume the most expensive known rate — the governor must
# never under-count.
FALLBACK_PRICE: tuple[float, float] = (5.0, 25.0)

BATCH_DISCOUNT = 0.5      # Message Batches: 50% off all token usage
CACHE_READ_FACTOR = 0.1   # cache reads ~0.1x the input rate

# v8: investigation spend lives in its OWN daily envelope. These ledger
# purposes are summed by the investigation governor and EXCLUDED from the
# general governor — neither budget gates or charges the other.
INVESTIGATION_PURPOSES: tuple[str, ...] = (
    "investigation", "investigation_t1", "investigation_synthesis")

# T1 cost-projection assumptions (design doc: ~2.2K prompt+content in,
# ~300 structured out per item on the FAST tier).
EST_T1_INPUT_TOKENS = 2200
EST_T1_OUTPUT_TOKENS = 300


def price_for(model: str) -> tuple[float, float]:
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, price in PRICING.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = price, len(prefix)
    return best if best is not None else FALLBACK_PRICE


def cost_usd(model: str, *, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, batch: bool = False) -> float:
    """USD cost of one call from token counts (MTok rates / 1e6)."""
    in_rate, out_rate = price_for(model)
    cost = (input_tokens * in_rate
            + output_tokens * out_rate
            + cache_read_tokens * in_rate * CACHE_READ_FACTOR) / 1_000_000
    return cost * BATCH_DISCOUNT if batch else cost


def estimated_t1_cost(model: str, *, batch: bool) -> float:
    """Projected cost of one T1 extraction (governor look-ahead)."""
    return cost_usd(model, input_tokens=EST_T1_INPUT_TOKENS,
                    output_tokens=EST_T1_OUTPUT_TOKENS, batch=batch)


async def record_call(conn: psycopg.AsyncConnection, *, purpose: str,
                      model: str, usage: Usage, batch: bool = False,
                      batch_id: str | None = None) -> int:
    """Append one llm_call ledger row; returns its id."""
    cost = cost_usd(model, input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens, batch=batch)
    # NO explicit transaction: a single INSERT is atomic under autocommit,
    # and this is the one DAO write that runs inside concurrent tasks
    # sharing a connection (verify.py's stance fan-out) — interleaved
    # transaction() blocks on one connection break at exit ("commit at the
    # wrong nesting level"), plain statements just serialize.
    cur = await conn.execute(
        "INSERT INTO llm_call (purpose, model, input_tokens,"
        " output_tokens, cache_read_tokens, batch_id, cost_estimate,"
        " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (purpose, model, usage.input_tokens, usage.output_tokens,
         usage.cache_read_tokens, batch_id, cost, utc_now()))
    row = await cur.fetchone()
    return int(row["id"])


async def spent_on(conn: psycopg.AsyncConnection, day: str, *,
                   purposes: tuple[str, ...] | None = None,
                   exclude_purposes: tuple[str, ...] = ()) -> float:
    """Total ledgered USD for one 'YYYY-MM-DD' UTC day, optionally scoped
    to (or excluding) a purpose set — the two-governor split is a WHERE
    clause, never a second ledger."""
    sql = ("SELECT COALESCE(SUM(cost_estimate), 0) AS total FROM llm_call"
           " WHERE (created_at AT TIME ZONE 'utc')::date = %s")
    params: list = [day]
    if purposes is not None:
        sql += " AND purpose = ANY(%s)"
        params.append(list(purposes))
    if exclude_purposes:
        sql += " AND NOT (purpose = ANY(%s))"
        params.append(list(exclude_purposes))
    cur = await conn.execute(sql, params)
    return float((await cur.fetchone())["total"])


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def spent_today(conn: psycopg.AsyncConnection, *,
                      purposes: tuple[str, ...] | None = None,
                      exclude_purposes: tuple[str, ...] = ()) -> float:
    return await spent_on(conn, today_utc(), purposes=purposes,
                          exclude_purposes=exclude_purposes)


async def daily_breakdown(conn: psycopg.AsyncConnection,
                          days: int) -> list[dict]:
    """Per-day ledger rollups for the last ``days`` UTC days (oldest first;
    days with no calls are zero-filled so charts get a dense series)."""
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    cur = await conn.execute(
        "SELECT (created_at AT TIME ZONE 'utc')::date AS day,"
        " COUNT(*) AS calls,"
        " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(cost_estimate), 0) AS cost_usd"
        " FROM llm_call"
        " WHERE (created_at AT TIME ZONE 'utc')::date >= %s"
        " GROUP BY day ORDER BY day", (start,))
    by_day = {r["day"]: r for r in await cur.fetchall()}
    out: list[dict] = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        r = by_day.get(day)
        out.append({
            "day": day,
            "calls": int(r["calls"]) if r else 0,
            "input_tokens": int(r["input_tokens"]) if r else 0,
            "output_tokens": int(r["output_tokens"]) if r else 0,
            "cost_usd": float(r["cost_usd"]) if r else 0.0,
        })
    return out


class BudgetExceeded(RuntimeError):
    """Today's ledger + the projected spend would exceed the daily budget."""


class Governor:
    """check() raises BudgetExceeded when ledger + projection > budget.

    ``purposes`` scopes the governor to those ledger purposes only (the
    investigation governor); ``exclude_purposes`` carves purposes OUT of an
    otherwise-global governor (the general governor excludes investigation
    spend). Defaults preserve the original whole-ledger behavior.

    Holds the POOL and reads the ledger on a per-check connection."""

    def __init__(self, pool: AsyncConnectionPool, daily_budget_usd: float,
                 *, purposes: tuple[str, ...] | None = None,
                 exclude_purposes: tuple[str, ...] = ()):
        self.pool = pool
        self.daily_budget_usd = daily_budget_usd
        self.purposes = purposes
        self.exclude_purposes = exclude_purposes

    async def spent_today(self) -> float:
        async with self.pool.connection() as conn:
            return await spent_today(
                conn, purposes=self.purposes,
                exclude_purposes=self.exclude_purposes)

    async def check(self, projected_usd: float = 0.0) -> None:
        spent = await self.spent_today()
        if spent + projected_usd > self.daily_budget_usd:
            raise BudgetExceeded(
                f"daily LLM budget exceeded: spent ${spent:.4f} + projected "
                f"${projected_usd:.4f} > ${self.daily_budget_usd:.2f}")
