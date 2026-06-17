"""Idempotent seed of built-in India sources — live-verified URLs (June 2026,
from the source-research pass). Seeds are ordinary registry rows, not
special-cased code; matching is by unique name, so user edits survive
restarts and re-seeding never duplicates.

Tiers: 1 official/primary · 2 established national media + fact-checkers ·
3 aggregator. Google News is t1_exempt (high volume, heavy syndication —
metadata-only in Phase 1 unless watch-hit).
"""

from __future__ import annotations

import psycopg

from connect.storage import sources as source_dao
from connect.storage.pg import Jsonb

SEED_SOURCES: tuple[dict, ...] = (
    {
        "name": "RBI Notifications",
        "type": "rss",
        "config": {"feed_url": "https://rbi.org.in/notifications_rss.xml",
                   "poll_interval_minutes": 60},
        "credibility_tier": 1,
        "notes": "Reserve Bank of India — circulars/notifications (verified live)",
    },
    {
        "name": "RBI Press Releases",
        "type": "rss",
        "config": {"feed_url": "https://rbi.org.in/pressreleases_rss.xml",
                   "poll_interval_minutes": 60},
        "credibility_tier": 1,
        "notes": "Reserve Bank of India — press releases (verified live)",
    },
    {
        "name": "PIB Press Releases",
        "type": "rss",
        "config": {"feed_url": "https://www.pib.gov.in/RssMain.aspx"
                               "?ModId=6&Lang=1&Regid=3&reg=3",
                   "poll_interval_minutes": 60},
        "credibility_tier": 1,
        "notes": "Press Information Bureau, GoI (English press releases). "
                 "NIC 403s bot UAs — fetcher sends a browser UA. "
                 "ViewRss.aspx began returning HTML (June 2026); RssMain.aspx "
                 "with explicit reg=3 is the working English feed.",
    },
    {
        "name": "Alt News",
        "type": "rss",
        "config": {"feed_url": "https://www.altnews.in/feed/",
                   "poll_interval_minutes": 120},
        "credibility_tier": 2,
        "notes": "Fact-checker (verified live). Always-promote source in Phase 1+.",
    },
    {
        "name": "BOOM Live",
        "type": "rss",
        "config": {"feed_url": "https://www.boomlive.in/feed",
                   "poll_interval_minutes": 120},
        "credibility_tier": 2,
        "notes": "Fact-checker (verified live). Always-promote source in Phase 1+.",
    },
    {
        "name": "Economic Times - Economy",
        "type": "rss",
        "config": {"feed_url": "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms",
                   "poll_interval_minutes": 60},
        "credibility_tier": 2,
        "notes": "ET economy section feed (feed id verified via aggregator).",
    },
    {
        "name": "Moneycontrol News",
        "type": "web_news",
        "config": {
            "index_url": "https://www.moneycontrol.com/news/",
            "link_pattern": r"moneycontrol\.com/news/[a-z][a-z0-9/\-]+-\d+\.html",
            "poll_interval_minutes": 60,
        },
        "credibility_tier": 2,
        "notes": "Moneycontrol news index — HTML scraping (no RSS). "
                 "link_pattern matches article URLs (slug + numeric id + .html) "
                 "and excludes nav/category links. Tier 2 (established national "
                 "financial media).",
    },
    # --- SEBI (tier 1) ----------------------------------------------------------
    {
        "name": "SEBI Press Releases",
        "type": "rss",
        "config": {"feed_url": "https://www.sebi.gov.in/sebirss.xml",
                   "poll_interval_minutes": 60},
        "credibility_tier": 1,
        "notes": "Securities and Exchange Board of India — official circulars, "
                 "orders, press releases. URL needs live-verification on first deploy.",
    },
    # --- national media, tier 2 (RSS) -----------------------------------------
    {
        "name": "The Wire",
        "type": "rss",
        "config": {"feed_url": "https://thewire.in/feed",
                   "poll_interval_minutes": 60},
        "credibility_tier": 2,
        "notes": "Independent national news. URL needs live-verification on first deploy.",
    },
    {
        "name": "The Print",
        "type": "rss",
        "config": {"feed_url": "https://theprint.in/category/economy/feed/",
                   "poll_interval_minutes": 60},
        "credibility_tier": 2,
        "notes": "ThePrint economy category feed (live-verified June 2026; the "
                 "site-wide /feed/ 403s scrapers, the category feed does not).",
    },
    {
        "name": "The Hindu — Business",
        "type": "rss",
        "config": {"feed_url": "https://www.thehindu.com/business/Economy/?service=rss",
                   "poll_interval_minutes": 120},
        "credibility_tier": 2,
        "notes": "The Hindu economy/business section RSS. "
                 "URL needs live-verification on first deploy.",
    },
    {
        "name": "NDTV — Top Stories",
        "type": "rss",
        "config": {"feed_url": "https://feeds.feedburner.com/ndtvnews-top-stories",
                   "poll_interval_minutes": 60},
        "credibility_tier": 2,
        "notes": "NDTV top-stories feed via FeedBurner. "
                 "URL needs live-verification on first deploy.",
    },
    {
        "name": "India Today",
        "type": "rss",
        "config": {"feed_url": "https://www.indiatoday.in/feed",
                   "poll_interval_minutes": 60},
        "credibility_tier": 2,
        "notes": "India Today top feed. URL needs live-verification on first deploy.",
    },
    {
        "name": "Financial Express",
        "type": "web_news",
        "config": {
            "index_url": "https://www.financialexpress.com/economy/",
            "link_pattern": r"financialexpress\.com/[a-z-]+/[a-z0-9-]+-\d{6,}/",
            "poll_interval_minutes": 60,
        },
        "credibility_tier": 2,
        "notes": "Financial Express economy section — HTML scraping (live-verified "
                 "June 2026; FE discontinued RSS, /feed/ returns HTTP 410).",
    },
    {
        "name": "Factly",
        "type": "rss",
        "config": {"feed_url": "https://factly.in/feed/",
                   "poll_interval_minutes": 120},
        "credibility_tier": 2,
        "notes": "Fact-checker (data-driven, policy focus). "
                 "URL needs live-verification on first deploy.",
    },
    {
        "name": "Vishvas News",
        "type": "rss",
        "config": {"feed_url": "https://www.vishvasnews.com/feed/",
                   "poll_interval_minutes": 120},
        "credibility_tier": 2,
        "notes": "Fact-checker (Hindi/English). "
                 "URL needs live-verification on first deploy.",
    },
    # --- aggregator tier 3 (RSS, t1_exempt) ------------------------------------
    {
        "name": "Times of India — Top Stories",
        "type": "rss",
        "config": {"feed_url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
                   "poll_interval_minutes": 60},
        "credibility_tier": 3,
        "t1_exempt": True,
        "notes": "ToI top-stories feed; high volume + syndication => T1-exempt. "
                 "URL needs live-verification on first deploy.",
    },
    # --- web_news (tier 2, no RSS) --------------------------------------------
    {
        "name": "Business Standard — Economy",
        "type": "rss",
        "config": {"feed_url": "https://www.business-standard.com/rss/economy-102.rss",
                   "poll_interval_minutes": 360},
        "credibility_tier": 2,
        "notes": "Business Standard economy section RSS (live-verified June 2026). "
                 "The feed is reachable, but BS may 403 article pages from some "
                 "server IPs (no full text then); polled every 6h to stay gentle.",
    },
    {
        "name": "NDTV Profit — Markets",
        "type": "rss",
        "config": {"feed_url": "https://www.ndtvprofit.com/rss",
                   "poll_interval_minutes": 60},
        "credibility_tier": 2,
        "notes": "NDTV Profit (formerly BQ Prime / BloombergQuint) main feed "
                 "(live-verified June 2026; bqprime.com retired in the rebrand).",
    },
    {
        "name": "CNBCTV18 — Economy",
        "type": "web_news",
        "config": {
            "index_url": "https://www.cnbctv18.com/economy/",
            "link_pattern": r"cnbctv18\.com/[a-z-]+/[a-z0-9-]+-\d+\.htm",
            "poll_interval_minutes": 60,
        },
        "credibility_tier": 2,
        "notes": "CNBCTV18 economy section — HTML scraping. "
                 "URL needs live-verification on first deploy.",
    },
    {
        "name": "Hindustan Times — India",
        "type": "web_news",
        "config": {
            "index_url": "https://www.hindustantimes.com/india-news/",
            "link_pattern": r"hindustantimes\.com/[a-z-]+/[a-z0-9-]+-\d{10,}\.html",
            "poll_interval_minutes": 60,
        },
        "credibility_tier": 2,
        "notes": "Hindustan Times India section — HTML scraping. "
                 "URL needs live-verification on first deploy.",
    },
    # --------------------------------------------------------------------------
    {
        "name": "LiveMint News",
        "type": "rss",
        "config": {"feed_url": "https://www.livemint.com/rss/news",
                   "poll_interval_minutes": 60},
        "credibility_tier": 2,
        "notes": "LiveMint top news feed.",
    },
    {
        "name": "Scroll.in",
        "type": "rss",
        "config": {"feed_url": "https://feeds.feedburner.com/ScrollinArticles.rss",
                   "poll_interval_minutes": 120},
        "credibility_tier": 2,
        "notes": "Scroll.in articles (verified live).",
    },
    {
        "name": "Google News India",
        "type": "rss",
        "config": {"feed_url": "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
                   "poll_interval_minutes": 60},
        "credibility_tier": 3,
        "t1_exempt": True,
        "notes": "Aggregation fallback; links are redirects; heavy syndication "
                 "=> T1-exempt unless watch-hit.",
    },
    {
        "name": "X — Government institutional",
        "type": "twitter",
        "config": {"handles": [
                       "PMOIndia", "PIB_India", "PIBFactCheck", "FinMinIndia",
                       "RBI", "SEBI_India", "MEAIndia", "ECISVEEP",
                       "SpokespersonECI", "mygovindia", "NITIAayog",
                       "DPIITGoI", "DFS_India", "cbic_india", "IncomeTaxIndia",
                       "MLJ_GoI", "DoPTGoI", "HMOIndia", "CimGOI"],
                   "poll_interval_minutes": 30},
        "credibility_tier": 1,
        "notes": "Institutional GoI handles via twitterapi.io — ONE batched "
                 "advanced-search call per poll. Cleanly no-ops until "
                 "TWITTERAPI_IO_API_KEY is set.",
    },
    {
        "name": "X — Ministers personal",
        "type": "twitter",
        "config": {"handles": ["narendramodi", "nsitharaman", "mppchaudhary"],
                   "poll_interval_minutes": 30},
        "credibility_tier": 2,
        "notes": "Personal handles (PM, FM, MoS Finance) — announcements often "
                 "land here first; tier 2 (personal, not institutional). "
                 "Same single batched twitterapi.io call; no-ops without "
                 "TWITTERAPI_IO_API_KEY.",
    },
    {
        "name": "PIB Backgrounders (Telegram)",
        "type": "telegram",
        "config": {"channel": "PIB_Backgrounders",
                   "poll_interval_minutes": 30},
        "credibility_tier": 1,
        "notes": "PIB explainer/backgrounder channel via the free t.me/s "
                 "public preview — no key needed.",
    },
    {
        "name": "PIB Fact Check (Telegram)",
        "type": "telegram",
        "config": {"channel": "PIB_FactCheck",
                   "poll_interval_minutes": 30},
        "credibility_tier": 1,
        "notes": "PIB Fact Check channel via the free t.me/s public preview. "
                 "Always-promote source in Phase 1+.",
    },
    {
        "name": "MIB India (Telegram)",
        "type": "telegram",
        "config": {"channel": "MIB_India",
                   "poll_interval_minutes": 30},
        "credibility_tier": 1,
        "notes": "Ministry of Information & Broadcasting channel via the "
                 "free t.me/s public preview.",
    },
    {
        # v8 (investigation mode): every web doc an investigation fetches
        # attaches here at tier 4 (unverified) until manually promoted.
        # type='search' is not pollable — the row only provides provenance.
        "name": "Web (investigation)",
        "type": "search",
        "config": {},
        "credibility_tier": 4,
        "notes": "Investigation-fetched web documents (tier 4 until a "
                 "domain is promoted into the source registry).",
    },
)

WEB_INVESTIGATION_SOURCE = "Web (investigation)"


async def seed_sources(conn: psycopg.AsyncConnection) -> int:
    """Insert missing seed rows (matched by name); returns how many added."""
    added = 0
    for seed in SEED_SOURCES:
        if await source_dao.get_by_name(conn, seed["name"]) is not None:
            continue
        await source_dao.insert(
            conn,
            name=seed["name"],
            type_=seed["type"],
            config=seed["config"],
            credibility_tier=seed["credibility_tier"],
            notes=seed.get("notes"),
            t1_exempt=bool(seed.get("t1_exempt", False)),
        )
        added += 1
    return added


# Built-in sources whose originally-seeded URL went dead or got bot-blocked
# after the first research pass (the "live-verification" the notes flagged).
# Each repair is applied ONLY when the live row STILL carries the exact broken
# value under ``match`` — so a user's own edit is never clobbered, and the
# repair is idempotent (once applied, the match no longer holds). ``from_name``
# lets a repair also rename a row (BQ Prime -> NDTV Profit after the rebrand);
# it must run BEFORE seed_sources so the renamed row is found and the corrected
# SEED_SOURCES entry is not inserted as a duplicate.
SEED_REPAIRS: tuple[dict, ...] = (
    {
        "from_name": "Business Standard — Economy",
        "match": ("index_url", "https://www.business-standard.com/economy-policy"),
        "set": {
            "type": "rss",
            "config": {"feed_url": "https://www.business-standard.com/rss/economy-102.rss",
                       "poll_interval_minutes": 360},
            "notes": "Business Standard economy section RSS (live-verified June 2026). "
                     "The feed is reachable, but BS may 403 article pages from some "
                     "server IPs (no full text then); polled every 6h to stay gentle.",
        },
    },
    {
        "from_name": "BQ Prime — Economy",
        "match": ("index_url", "https://www.bqprime.com/economy"),
        "set": {
            "name": "NDTV Profit — Markets",
            "type": "rss",
            "config": {"feed_url": "https://www.ndtvprofit.com/rss",
                       "poll_interval_minutes": 60},
            "notes": "NDTV Profit (formerly BQ Prime / BloombergQuint) main feed "
                     "(live-verified June 2026; bqprime.com retired in the rebrand).",
        },
    },
    {
        "from_name": "The Print",
        "match": ("feed_url", "https://theprint.in/feed/"),
        "set": {
            "config": {"feed_url": "https://theprint.in/category/economy/feed/",
                       "poll_interval_minutes": 60},
            "notes": "ThePrint economy category feed (live-verified June 2026; the "
                     "site-wide /feed/ 403s scrapers, the category feed does not).",
        },
    },
    {
        "from_name": "Financial Express",
        "match": ("feed_url", "https://www.financialexpress.com/feed/"),
        "set": {
            "type": "web_news",
            "config": {"index_url": "https://www.financialexpress.com/economy/",
                       "link_pattern": r"financialexpress\.com/[a-z-]+/[a-z0-9-]+-\d{6,}/",
                       "poll_interval_minutes": 60},
            "notes": "Financial Express economy section — HTML scraping (live-verified "
                     "June 2026; FE discontinued RSS, /feed/ returns HTTP 410).",
        },
    },
)


async def repair_sources(conn: psycopg.AsyncConnection) -> int:
    """Correct built-in sources still carrying a known-broken seeded URL.

    Matches by ``from_name`` AND the exact broken config value, so a row a user
    has edited (or that was already repaired) is left untouched. Updates type /
    config / name / notes in one statement (``type`` is not patchable via the
    admin DAO, so this is a dedicated write) and clears the last poll result so
    the corrected source re-polls promptly. Returns how many rows were fixed.
    Must run BEFORE ``seed_sources``."""
    repaired = 0
    for spec in SEED_REPAIRS:
        row = await source_dao.get_by_name(conn, spec["from_name"])
        if row is None:
            continue
        key, broken = spec["match"]
        if (row.config or {}).get(key) != broken:
            continue  # user-edited or already repaired — never overwrite
        s = spec["set"]
        async with conn.transaction():
            await conn.execute(
                "UPDATE source SET name = %s, type = %s, config = %s,"
                " notes = %s, last_polled_at = NULL, last_poll_status = NULL"
                " WHERE id = %s",
                (s.get("name", row.name), s.get("type", row.type),
                 Jsonb(s["config"]), s.get("notes", row.notes), row.id))
        repaired += 1
    return repaired
