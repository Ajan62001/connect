"""Seed registry: idempotency (match-by-name) incl. the twitter/telegram
rows, and every seed config validates against its type's model."""

from __future__ import annotations

from connect.sources.base import parse_source_config
from connect.sources.seeds import SEED_SOURCES, repair_sources, seed_sources
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


async def test_seeding_is_idempotent(container, db):
    # container.startup() already seeded once
    before = {s.name: s.id for s in await source_dao.list_all(db)}
    assert NEW_SEED_NAMES <= set(before)

    assert await seed_sources(db) == 0  # nothing added on re-seed
    after = {s.name: s.id for s in await source_dao.list_all(db)}
    assert after == before

    # a user edit survives re-seeding (match is by name, not overwrite)
    gov = await source_dao.get_by_name(db, "X — Government institutional")
    await source_dao.update(db, gov.id, {"credibility_tier": 2})
    assert await seed_sources(db) == 0
    assert (await source_dao.get(db, gov.id)).credibility_tier == 2


async def test_repair_fixes_broken_builtin_sources(container, db):
    """repair_sources corrects built-in rows still carrying a dead/blocked URL,
    renames BQ Prime -> NDTV Profit, clears the poll error, is idempotent, and
    never overwrites a user's own edit."""
    # simulate an older deploy: drop the corrected seeds, plant the broken ones
    for name in ("Business Standard — Economy", "NDTV Profit — Markets",
                 "The Print", "Financial Express", "BQ Prime — Economy"):
        s = await source_dao.get_by_name(db, name)
        if s is not None:
            await source_dao.delete(db, s.id)
    await source_dao.insert(
        db, name="Business Standard — Economy", type_="web_news",
        config={"index_url": "https://www.business-standard.com/economy-policy",
                "link_pattern": r"x", "poll_interval_minutes": 60},
        credibility_tier=2)
    await source_dao.insert(
        db, name="BQ Prime — Economy", type_="web_news",
        config={"index_url": "https://www.bqprime.com/economy",
                "link_pattern": r"x"}, credibility_tier=2)
    await source_dao.insert(
        db, name="The Print", type_="rss",
        config={"feed_url": "https://theprint.in/feed/"}, credibility_tier=2)
    await source_dao.insert(
        db, name="Financial Express", type_="rss",
        config={"feed_url": "https://www.financialexpress.com/feed/"},
        credibility_tier=2)
    # a stale poll error that the repair should clear
    await db.execute(
        "UPDATE source SET last_poll_status = 'error: HTTP 403'"
        " WHERE name = 'The Print'")

    assert await repair_sources(db) == 4

    bs = await source_dao.get_by_name(db, "Business Standard — Economy")
    assert bs.type == "rss" and "economy-102.rss" in bs.config["feed_url"]

    # BQ Prime is renamed to NDTV Profit and repointed at the working feed
    assert await source_dao.get_by_name(db, "BQ Prime — Economy") is None
    ndtv = await source_dao.get_by_name(db, "NDTV Profit — Markets")
    assert ndtv is not None and ndtv.type == "rss"
    assert "ndtvprofit.com/rss" in ndtv.config["feed_url"]

    tp = await source_dao.get_by_name(db, "The Print")
    assert "category/economy/feed" in tp.config["feed_url"]
    assert tp.last_poll_status is None  # error cleared so the UI recovers

    fe = await source_dao.get_by_name(db, "Financial Express")
    assert fe.type == "web_news"
    assert "economy" in fe.config["index_url"]

    # idempotent: nothing left to repair
    assert await repair_sources(db) == 0

    # a user edit is never clobbered: a row whose feed_url is not the known
    # broken value is left untouched
    await source_dao.update(
        db, tp.id, {"config": {"feed_url": "https://theprint.in/category/"
                                           "india/feed/"}})
    assert await repair_sources(db) == 0
    again = await source_dao.get_by_name(db, "The Print")
    assert again.config["feed_url"] == "https://theprint.in/category/india/feed/"


async def test_twitter_seed_rows(container, db):
    gov = await source_dao.get_by_name(
        db, "X — Government institutional")
    assert gov.type == "twitter"
    assert gov.credibility_tier == 1
    assert gov.enabled is True  # relies on the graceful key-missing no-op
    handles = gov.config["handles"]
    assert len(handles) == 19
    assert {"PMOIndia", "PIB_India", "PIBFactCheck", "RBI", "SEBI_India",
            "IncomeTaxIndia", "CimGOI"} <= set(handles)
    assert gov.config["poll_interval_minutes"] == 30

    ministers = await source_dao.get_by_name(
        db, "X — Ministers personal")
    assert ministers.type == "twitter"
    assert ministers.credibility_tier == 2
    assert ministers.config["handles"] == [
        "narendramodi", "nsitharaman", "mppchaudhary"]


async def test_app_setting_budget_seeds(container, db):
    """Locked budgets land in app_setting at startup (member $0.50/$2,
    global backstop $10) — and an admin edit survives re-seeding."""
    from connect.storage import app_settings as app_settings_dao

    assert await app_settings_dao.get(
        db, "member_daily_budget_usd") == "0.5"
    assert await app_settings_dao.get(
        db, "member_investigation_daily_budget_usd") == "2.0"
    assert await app_settings_dao.get(
        db, "global_daily_budget_usd") == "10.0"

    # admin edit (the admin UI is the only writer) survives a restart's
    # re-seed: ON CONFLICT DO NOTHING
    await db.execute(
        "UPDATE app_setting SET value = '25.0'"
        " WHERE key = 'global_daily_budget_usd'")
    assert await app_settings_dao.seed_defaults(
        db, container.settings) == 0
    assert await app_settings_dao.get(
        db, "global_daily_budget_usd") == "25.0"


async def test_telegram_seed_rows(container, db):
    expected = {
        "PIB Backgrounders (Telegram)": "PIB_Backgrounders",
        "PIB Fact Check (Telegram)": "PIB_FactCheck",
        "MIB India (Telegram)": "MIB_India",
    }
    for name, channel in expected.items():
        seed = await source_dao.get_by_name(db, name)
        assert seed.type == "telegram"
        assert seed.credibility_tier == 1
        assert seed.enabled is True
        assert seed.config["channel"] == channel
