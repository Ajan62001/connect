"""The SEPARATE investigation budget envelope: the $10/day investigation
governor sums ONLY investigation* ledger purposes, the $2/day general
governor excludes them — investigation spend never trips the general
governor and vice versa. Wiring is verified on the composition root."""

from __future__ import annotations

import pytest

from connect.llm import spend
from connect.llm.provider import Usage
from connect.llm.spend import (
    INVESTIGATION_PURPOSES,
    BudgetExceeded,
    Governor,
)

BIG = Usage(input_tokens=1_000_000, output_tokens=0)  # $3 on sonnet


async def ledger(conn, purpose, dollars):
    """Insert one llm_call row of approximately `dollars` cost."""
    tokens = int(dollars / 3.0 * 1_000_000)  # sonnet input rate $3/MTok
    await spend.record_call(conn, purpose=purpose,
                            model="claude-sonnet-4-6",
                            usage=Usage(input_tokens=tokens))


async def test_purpose_scoped_spend_accounting(db):
    conn = db
    await ledger(conn, "investigation", 4.0)
    await ledger(conn, "investigation_t1", 1.0)
    await ledger(conn, "investigation_synthesis", 0.5)
    await ledger(conn, "analysis", 1.5)
    await ledger(conn, "enrich_t1", 0.25)

    total = await spend.spent_today(conn)
    inv = await spend.spent_today(conn, purposes=INVESTIGATION_PURPOSES)
    general = await spend.spent_today(
        conn, exclude_purposes=INVESTIGATION_PURPOSES)
    assert total == pytest.approx(7.25, abs=0.01)
    assert inv == pytest.approx(5.5, abs=0.01)
    assert general == pytest.approx(1.75, abs=0.01)
    assert inv + general == pytest.approx(total, abs=0.001)


async def test_investigation_spend_does_not_trip_general_governor(db, pool):
    conn = db
    general = Governor(pool, 2.0, exclude_purposes=INVESTIGATION_PURPOSES)
    investigation = Governor(pool, 10.0, purposes=INVESTIGATION_PURPOSES)

    # $9 of investigation spend: general unaffected, investigation near cap
    for _ in range(3):
        await ledger(conn, "investigation", 3.0)
    await general.check(1.0)        # passes — would trip a naive governor
    await investigation.check(0.5)  # 9 + 0.5 < 10 still fits
    with pytest.raises(BudgetExceeded):
        await investigation.check(1.5)    # 9 + 1.5 > 10


async def test_general_spend_does_not_trip_investigation_governor(db, pool):
    conn = db
    general = Governor(pool, 2.0, exclude_purposes=INVESTIGATION_PURPOSES)
    investigation = Governor(pool, 10.0, purposes=INVESTIGATION_PURPOSES)

    await ledger(conn, "analysis", 1.9)
    await ledger(conn, "enrich_t1", 0.3)  # general now over $2
    with pytest.raises(BudgetExceeded):
        await general.check(0.0)
    await investigation.check(9.9)  # the whole envelope is still free


async def test_unscoped_governor_keeps_legacy_whole_ledger_behavior(db,
                                                                    pool):
    conn = db
    await ledger(conn, "investigation", 1.5)
    await ledger(conn, "analysis", 1.0)
    legacy = Governor(pool, 2.0)    # no scoping args: sums everything
    with pytest.raises(BudgetExceeded):
        await legacy.check(0.0)


async def test_container_wires_both_governors(container):
    general = container.governor
    investigation = container.investigation_governor
    assert general.daily_budget_usd == 2.0
    assert general.exclude_purposes == INVESTIGATION_PURPOSES
    assert general.purposes is None
    assert investigation.daily_budget_usd == 10.0
    assert investigation.purposes == INVESTIGATION_PURPOSES
    # the investigation service runs on the investigation governor,
    # the analysis service on the general one
    assert container.investigations.governor is investigation
    assert container.analysis.governor is general
