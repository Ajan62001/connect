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

Phase D (tenancy design §4): the ledger carries ``user_id`` and the
``TenantGovernor`` layers a per-user daily ceiling and the deployment-wide
GLOBAL backstop over the existing purpose Governors. Attribution flows
through ONE choke point — ``CURRENT_USER_ID`` is set by the worker around
job execution from ``job.owner_id`` (workers/queue.execute_job), so every
governed call site inside a job charges the job's owner without threading
a parameter through every helper; API request paths (pre-accept checks)
pass the user explicitly instead. System work (polls, nightly sweeps,
automatic T2 triggers) runs with no ambient user and charges only the
global + purpose envelopes.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg_pool import AsyncConnectionPool

from connect.llm.provider import Usage
from connect.storage.pg import utc_now

# The ambient acting user (ledger attribution + per-user budget gating).
# Set exactly once per job execution from job.owner_id; None = system.
CURRENT_USER_ID: ContextVar[int | None] = ContextVar(
    "connect_current_user_id", default=None)

# "user_id not passed — use the ambient job owner" sentinel (None must stay
# expressible: it means system scope explicitly).
_AMBIENT = object()

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
                      batch_id: str | None = None,
                      user_id: int | None = None) -> int:
    """Append one llm_call ledger row; returns its id.

    ``user_id`` None falls back to the ambient job owner (CURRENT_USER_ID)
    — the attribution rule of tenancy design §4: user-initiated jobs
    (analysis/investigation/manual promote) charge their owner, system
    jobs stay NULL."""
    if user_id is None:
        user_id = CURRENT_USER_ID.get()
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
        " created_at, user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " RETURNING id",
        (purpose, model, usage.input_tokens, usage.output_tokens,
         usage.cache_read_tokens, batch_id, cost, utc_now(), user_id))
    row = await cur.fetchone()
    return int(row["id"])


async def spent_on(conn: psycopg.AsyncConnection, day: str, *,
                   purposes: tuple[str, ...] | None = None,
                   exclude_purposes: tuple[str, ...] = (),
                   user_id: int | None = None) -> float:
    """Total ledgered USD for one 'YYYY-MM-DD' UTC day, optionally scoped
    to (or excluding) a purpose set — the two-governor split is a WHERE
    clause, never a second ledger. ``user_id`` narrows to one user's
    spend (None = the whole ledger, system rows included)."""
    sql = ("SELECT COALESCE(SUM(cost_estimate), 0) AS total FROM llm_call"
           " WHERE (created_at AT TIME ZONE 'utc')::date = %s")
    params: list = [day]
    if purposes is not None:
        sql += " AND purpose = ANY(%s)"
        params.append(list(purposes))
    if exclude_purposes:
        sql += " AND NOT (purpose = ANY(%s))"
        params.append(list(exclude_purposes))
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    cur = await conn.execute(sql, params)
    return float((await cur.fetchone())["total"])


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def spent_today(conn: psycopg.AsyncConnection, *,
                      purposes: tuple[str, ...] | None = None,
                      exclude_purposes: tuple[str, ...] = (),
                      user_id: int | None = None) -> float:
    return await spent_on(conn, today_utc(), purposes=purposes,
                          exclude_purposes=exclude_purposes,
                          user_id=user_id)


def zero_day(day: str) -> dict:
    return {"day": day, "calls": 0, "input_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0}


def window_days(days: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=offset)).isoformat()
            for offset in range(days - 1, -1, -1)]


async def daily_breakdown(conn: psycopg.AsyncConnection, days: int, *,
                          user_id: int | None = None) -> list[dict]:
    """Per-day ledger rollups for the last ``days`` UTC days (oldest first;
    days with no calls are zero-filled so charts get a dense series).
    ``user_id`` narrows to one user's rows (None = whole ledger)."""
    window = window_days(days)
    sql = ("SELECT (created_at AT TIME ZONE 'utc')::date AS day,"
           " COUNT(*) AS calls,"
           " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
           " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
           " COALESCE(SUM(cost_estimate), 0) AS cost_usd"
           " FROM llm_call"
           " WHERE (created_at AT TIME ZONE 'utc')::date >= %s")
    params: list = [window[0]]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    cur = await conn.execute(sql + " GROUP BY day ORDER BY day", params)
    by_day = {r["day"]: r for r in await cur.fetchall()}
    out: list[dict] = []
    for day in window:
        r = by_day.get(day)
        out.append({
            "day": day,
            "calls": int(r["calls"]) if r else 0,
            "input_tokens": int(r["input_tokens"]) if r else 0,
            "output_tokens": int(r["output_tokens"]) if r else 0,
            "cost_usd": float(r["cost_usd"]) if r else 0.0,
        } if r else zero_day(day))
    return out


async def per_user_daily_breakdown(conn: psycopg.AsyncConnection,
                                   days: int) -> dict[int | None,
                                                      list[dict]]:
    """The admin spend split (design §4): per-user per-day rollups over the
    last ``days`` UTC days, zero-filled like daily_breakdown. Key None =
    system rows (polls, nightly sweeps — user_id IS NULL)."""
    window = window_days(days)
    cur = await conn.execute(
        "SELECT user_id, (created_at AT TIME ZONE 'utc')::date AS day,"
        " COUNT(*) AS calls,"
        " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(cost_estimate), 0) AS cost_usd"
        " FROM llm_call"
        " WHERE (created_at AT TIME ZONE 'utc')::date >= %s"
        " GROUP BY user_id, day ORDER BY user_id, day", (window[0],))
    grouped: dict[int | None, dict[str, dict]] = {}
    for r in await cur.fetchall():
        uid = int(r["user_id"]) if r["user_id"] is not None else None
        grouped.setdefault(uid, {})[r["day"]] = {
            "day": r["day"], "calls": int(r["calls"]),
            "input_tokens": int(r["input_tokens"]),
            "output_tokens": int(r["output_tokens"]),
            "cost_usd": float(r["cost_usd"])}
    return {uid: [by_day.get(day) or zero_day(day) for day in window]
            for uid, by_day in grouped.items()}


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


# --- the tenant layer (tenancy design §4, Phase D) -----------------------------

# app_setting keys (admin-editable; env values are seeds/fallbacks — the
# "app_setting wins when present" rule of design §9)
SETTING_GLOBAL_BUDGET = "global_daily_budget_usd"
SETTING_GENERAL_ENVELOPE = "daily_llm_budget_usd"
SETTING_INVESTIGATION_ENVELOPE = "investigation_daily_budget_usd"
SETTING_MEMBER_GENERAL = "member_daily_budget_usd"
SETTING_MEMBER_INVESTIGATION = "member_investigation_daily_budget_usd"

GENERAL_SCOPE = "general"
INVESTIGATION_SCOPE = "investigation"


class TenantGovernor(Governor):
    """The layered budget gate, a drop-in Governor replacement.

    ``check(projected, user_id=...)`` enforces THREE ceilings in order:

    1. the GLOBAL deployment backstop — the whole ledger vs the
       ``global_daily_budget_usd`` app_setting (env seed: locked $10/day);
       this is the hard cap on the actual Anthropic bill;
    2. the purpose envelope — the v0.1 Governor behavior (general excludes
       investigation purposes; investigation sees only them), with the
       budget now adjustable via app_setting without a redeploy;
    3. the per-user daily ceiling — the app_user override column when set,
       else the effective purpose envelope for admins ("admin keeps
       $2/$10"), else the member default app_setting (locked $0.50
       general / $2.00 investigation).

    ``user_id`` defaults to the ambient job owner (CURRENT_USER_ID), so the
    deep call sites — AnalysisContext, the investigation loop, T1/T2 —
    keep calling ``check(projected)`` unchanged and still get per-user
    enforcement inside user-owned jobs. Passing ``user_id=None`` explicitly
    means system scope (layers 1+2 only). Budgets are read per check (one
    cheap PK lookup next to the ledger SUM), so an admin PATCH takes effect
    immediately in every process."""

    def __init__(self, pool: AsyncConnectionPool, settings, *, scope: str):
        assert scope in (GENERAL_SCOPE, INVESTIGATION_SCOPE), scope
        if scope == GENERAL_SCOPE:
            super().__init__(pool, settings.daily_llm_budget_usd,
                             exclude_purposes=INVESTIGATION_PURPOSES)
            self._envelope_key = SETTING_GENERAL_ENVELOPE
            self._member_key = SETTING_MEMBER_GENERAL
            self._member_default = settings.member_daily_budget_usd
            self._override_column = "daily_budget_usd"
        else:
            super().__init__(pool, settings.investigation_daily_budget_usd,
                             purposes=INVESTIGATION_PURPOSES)
            self._envelope_key = SETTING_INVESTIGATION_ENVELOPE
            self._member_key = SETTING_MEMBER_INVESTIGATION
            self._member_default = (
                settings.member_investigation_daily_budget_usd)
            self._override_column = "investigation_daily_budget_usd"
        self.scope = scope
        self.settings = settings

    async def _budget_settings(self, conn: psycopg.AsyncConnection,
                               ) -> dict[str, float]:
        """The admin-editable budget values, app_setting first."""
        cur = await conn.execute(
            "SELECT key, value FROM app_setting WHERE key = ANY(%s)",
            ([SETTING_GLOBAL_BUDGET, self._envelope_key,
              self._member_key],))
        out: dict[str, float] = {}
        for row in await cur.fetchall():
            try:
                out[row["key"]] = float(row["value"])
            except (TypeError, ValueError):
                continue  # malformed row: fall back to the env value
        return out

    def _effective(self, cfg: dict[str, float], key: str,
                   fallback: float) -> float:
        return cfg.get(key, fallback)

    async def user_cap(self, conn: psycopg.AsyncConnection, user_id: int,
                       cfg: dict[str, float] | None = None) -> float:
        """One user's effective daily ceiling for this scope:
        override column > (admin: purpose envelope) > member default."""
        if cfg is None:
            cfg = await self._budget_settings(conn)
        cur = await conn.execute(
            f"SELECT role, {self._override_column} AS override"
            f" FROM app_user WHERE id = %s", (user_id,))
        row = await cur.fetchone()
        if row is None:  # unknown user: the conservative member default
            return self._effective(cfg, self._member_key,
                                   self._member_default)
        if row["override"] is not None:
            return float(row["override"])
        if row["role"] == "admin":
            return self._effective(cfg, self._envelope_key,
                                   self.daily_budget_usd)
        return self._effective(cfg, self._member_key, self._member_default)

    async def check(self, projected_usd: float = 0.0,
                    user_id: int | None | object = _AMBIENT) -> None:
        if user_id is _AMBIENT:
            user_id = CURRENT_USER_ID.get()
        async with self.pool.connection() as conn:
            cfg = await self._budget_settings(conn)
            # 1. the GLOBAL backstop — whole ledger, every purpose and user
            global_cap = self._effective(
                cfg, SETTING_GLOBAL_BUDGET,
                self.settings.global_daily_budget_usd)
            total = await spent_today(conn)
            if total + projected_usd > global_cap:
                raise BudgetExceeded(
                    f"the deployment's daily LLM budget is exhausted"
                    f" (${total:.2f} of ${global_cap:.2f} used today) —"
                    f" try again tomorrow")
            # 2. the purpose envelope (v0.1 message format preserved)
            envelope_cap = self._effective(cfg, self._envelope_key,
                                           self.daily_budget_usd)
            spent = await spent_today(
                conn, purposes=self.purposes,
                exclude_purposes=self.exclude_purposes)
            if spent + projected_usd > envelope_cap:
                raise BudgetExceeded(
                    f"daily LLM budget exceeded: spent ${spent:.4f}"
                    f" + projected ${projected_usd:.4f}"
                    f" > ${envelope_cap:.2f}")
            # 3. the per-user ceiling
            if user_id is None:
                return
            assert isinstance(user_id, int), user_id
            cap = await self.user_cap(conn, user_id, cfg=cfg)
            mine = await spent_today(
                conn, purposes=self.purposes,
                exclude_purposes=self.exclude_purposes, user_id=user_id)
            if mine + projected_usd > cap:
                remaining = max(0.0, cap - mine)
                # two honest shapes: the ceiling is genuinely spent, or the
                # remainder simply cannot cover this request's projection
                # ("used up: $0.00 spent, $0.02 remaining" reads absurd)
                if remaining < 0.005:
                    raise BudgetExceeded(
                        f"your daily {self.scope} budget (${cap:.2f}) is"
                        f" used up (${mine:.2f} spent today) — try again"
                        f" tomorrow or ask an admin to raise your limit")
                raise BudgetExceeded(
                    f"your daily {self.scope} budget (${cap:.2f}) cannot"
                    f" cover this (~${projected_usd:.2f} projected,"
                    f" ${remaining:.2f} remaining today) — try a smaller"
                    f" run, wait for tomorrow, or ask an admin to raise"
                    f" your limit")
