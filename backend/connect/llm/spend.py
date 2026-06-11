"""The spend ledger + budget governor — cost control is structural.

Every LLM response's usage is recorded as an llm_call row with a computed
cost; the Governor compares today's ledger + a projection against the daily
budget BEFORE work is queued, so an over-budget day degrades to T0-only
(documents stay 'pending', never lost).

PRICING is the single editable home for per-model USD/MTok rates (verified
against the current Anthropic catalog via the claude-api skill, 2026-06):
haiku-4-5 1/5, sonnet-4-6 3/15, opus-4-8 5/25; Message Batches are 50% off;
cache reads bill at ~0.1x the input rate.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from connect.llm.provider import Usage
from connect.storage.db import utc_now

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


def record_call(conn: sqlite3.Connection, *, purpose: str, model: str,
                usage: Usage, batch: bool = False,
                batch_id: str | None = None) -> int:
    """Append one llm_call ledger row; returns its id."""
    cost = cost_usd(model, input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens, batch=batch)
    with conn:
        cur = conn.execute(
            "INSERT INTO llm_call (purpose, model, input_tokens,"
            " output_tokens, cache_read_tokens, batch_id, cost_estimate,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (purpose, model, usage.input_tokens, usage.output_tokens,
             usage.cache_read_tokens, batch_id, cost, utc_now()))
    return int(cur.lastrowid)  # type: ignore[arg-type]


def spent_on(conn: sqlite3.Connection, day: str, *,
             purposes: tuple[str, ...] | None = None,
             exclude_purposes: tuple[str, ...] = ()) -> float:
    """Total ledgered USD for one 'YYYY-MM-DD' UTC day, optionally scoped
    to (or excluding) a purpose set — the two-governor split is a WHERE
    clause, never a second ledger."""
    sql = ("SELECT COALESCE(SUM(cost_estimate), 0) FROM llm_call"
           " WHERE substr(created_at, 1, 10) = ?")
    params: list = [day]
    if purposes is not None:
        sql += f" AND purpose IN ({','.join('?' * len(purposes))})"
        params.extend(purposes)
    if exclude_purposes:
        sql += f" AND purpose NOT IN ({','.join('?' * len(exclude_purposes))})"
        params.extend(exclude_purposes)
    row = conn.execute(sql, params).fetchone()
    return float(row[0])


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def spent_today(conn: sqlite3.Connection, *,
                purposes: tuple[str, ...] | None = None,
                exclude_purposes: tuple[str, ...] = ()) -> float:
    return spent_on(conn, today_utc(), purposes=purposes,
                    exclude_purposes=exclude_purposes)


def daily_breakdown(conn: sqlite3.Connection, days: int) -> list[dict]:
    """Per-day ledger rollups for the last ``days`` UTC days (oldest first;
    days with no calls are zero-filled so charts get a dense series)."""
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day,"
        " COUNT(*) AS calls,"
        " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(cost_estimate), 0) AS cost_usd"
        " FROM llm_call"
        " WHERE substr(created_at, 1, 10) >= date('now', ?)"
        " GROUP BY day ORDER BY day", (f"-{days - 1} days",)).fetchall()
    by_day = {r["day"]: r for r in rows}
    out: list[dict] = []
    for offset in range(days - 1, -1, -1):
        day = conn.execute(
            "SELECT date('now', ?)", (f"-{offset} days",)).fetchone()[0]
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
    spend). Defaults preserve the original whole-ledger behavior."""

    def __init__(self, conn: sqlite3.Connection, daily_budget_usd: float, *,
                 purposes: tuple[str, ...] | None = None,
                 exclude_purposes: tuple[str, ...] = ()):
        self.conn = conn
        self.daily_budget_usd = daily_budget_usd
        self.purposes = purposes
        self.exclude_purposes = exclude_purposes

    def spent_today(self) -> float:
        return spent_today(self.conn, purposes=self.purposes,
                           exclude_purposes=self.exclude_purposes)

    def check(self, projected_usd: float = 0.0) -> None:
        spent = self.spent_today()
        if spent + projected_usd > self.daily_budget_usd:
            raise BudgetExceeded(
                f"daily LLM budget exceeded: spent ${spent:.4f} + projected "
                f"${projected_usd:.4f} > ${self.daily_budget_usd:.2f}")
