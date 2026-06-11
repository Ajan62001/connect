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
from connect.storage import db as db_mod

BIG = Usage(input_tokens=1_000_000, output_tokens=0)  # $3 on sonnet


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "t.db")
    db_mod.init_db(c)
    yield c
    c.close()


def ledger(conn, purpose, dollars):
    """Insert one llm_call row of approximately `dollars` cost."""
    tokens = int(dollars / 3.0 * 1_000_000)  # sonnet input rate $3/MTok
    spend.record_call(conn, purpose=purpose, model="claude-sonnet-4-6",
                      usage=Usage(input_tokens=tokens))


def test_purpose_scoped_spend_accounting(conn):
    ledger(conn, "investigation", 4.0)
    ledger(conn, "investigation_t1", 1.0)
    ledger(conn, "investigation_synthesis", 0.5)
    ledger(conn, "analysis", 1.5)
    ledger(conn, "enrich_t1", 0.25)

    total = spend.spent_today(conn)
    inv = spend.spent_today(conn, purposes=INVESTIGATION_PURPOSES)
    general = spend.spent_today(conn,
                                exclude_purposes=INVESTIGATION_PURPOSES)
    assert total == pytest.approx(7.25, abs=0.01)
    assert inv == pytest.approx(5.5, abs=0.01)
    assert general == pytest.approx(1.75, abs=0.01)
    assert inv + general == pytest.approx(total, abs=0.001)


def test_investigation_spend_does_not_trip_general_governor(conn):
    general = Governor(conn, 2.0, exclude_purposes=INVESTIGATION_PURPOSES)
    investigation = Governor(conn, 10.0, purposes=INVESTIGATION_PURPOSES)

    # $9 of investigation spend: general unaffected, investigation near cap
    for _ in range(3):
        ledger(conn, "investigation", 3.0)
    general.check(1.0)              # passes — would trip a naive governor
    investigation.check(0.5)        # 9 + 0.5 < 10 still fits
    with pytest.raises(BudgetExceeded):
        investigation.check(1.5)    # 9 + 1.5 > 10


def test_general_spend_does_not_trip_investigation_governor(conn):
    general = Governor(conn, 2.0, exclude_purposes=INVESTIGATION_PURPOSES)
    investigation = Governor(conn, 10.0, purposes=INVESTIGATION_PURPOSES)

    ledger(conn, "analysis", 1.9)
    ledger(conn, "enrich_t1", 0.3)  # general now over $2
    with pytest.raises(BudgetExceeded):
        general.check(0.0)
    investigation.check(9.9)        # the whole envelope is still free


def test_unscoped_governor_keeps_legacy_whole_ledger_behavior(conn):
    ledger(conn, "investigation", 1.5)
    ledger(conn, "analysis", 1.0)
    legacy = Governor(conn, 2.0)    # no scoping args: sums everything
    with pytest.raises(BudgetExceeded):
        legacy.check(0.0)


def test_container_wires_both_governors(container):
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
