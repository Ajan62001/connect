"""Seed registry: idempotency (match-by-name) incl. the twitter/telegram
rows, and every seed config validates against its type's model."""

from __future__ import annotations

from connect.sources.base import parse_source_config
from connect.sources.seeds import SEED_SOURCES, seed_sources
from connect.storage import sources as source_dao

NEW_SEED_NAMES = {
    "X — Government institutional",
    "X — Ministers personal",
    "PIB Backgrounders (Telegram)",
    "PIB Fact Check (Telegram)",
    "MIB India (Telegram)",
}


def test_all_seed_configs_validate():
    for seed in SEED_SOURCES:
        parse_source_config(seed["type"], seed["config"])  # must not raise


def test_seeding_is_idempotent(container):
    # container.startup() already seeded once
    before = {s.name: s.id for s in source_dao.list_all(container.db)}
    assert NEW_SEED_NAMES <= set(before)

    assert seed_sources(container.db) == 0  # nothing added on re-seed
    after = {s.name: s.id for s in source_dao.list_all(container.db)}
    assert after == before

    # a user edit survives re-seeding (match is by name, not overwrite)
    gov = source_dao.get_by_name(container.db, "X — Government institutional")
    source_dao.update(container.db, gov.id, {"credibility_tier": 2})
    assert seed_sources(container.db) == 0
    assert source_dao.get(container.db, gov.id).credibility_tier == 2


def test_twitter_seed_rows(container):
    gov = source_dao.get_by_name(
        container.db, "X — Government institutional")
    assert gov.type == "twitter"
    assert gov.credibility_tier == 1
    assert gov.enabled is True  # relies on the graceful key-missing no-op
    handles = gov.config["handles"]
    assert len(handles) == 19
    assert {"PMOIndia", "PIB_India", "PIBFactCheck", "RBI", "SEBI_India",
            "IncomeTaxIndia", "CimGOI"} <= set(handles)
    assert gov.config["poll_interval_minutes"] == 30

    ministers = source_dao.get_by_name(
        container.db, "X — Ministers personal")
    assert ministers.type == "twitter"
    assert ministers.credibility_tier == 2
    assert ministers.config["handles"] == [
        "narendramodi", "nsitharaman", "mppchaudhary"]


def test_telegram_seed_rows(container):
    expected = {
        "PIB Backgrounders (Telegram)": "PIB_Backgrounders",
        "PIB Fact Check (Telegram)": "PIB_FactCheck",
        "MIB India (Telegram)": "MIB_India",
    }
    for name, channel in expected.items():
        seed = source_dao.get_by_name(container.db, name)
        assert seed.type == "telegram"
        assert seed.credibility_tier == 1
        assert seed.enabled is True
        assert seed.config["channel"] == channel
