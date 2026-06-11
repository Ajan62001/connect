"""Idempotent seed of built-in India sources — live-verified URLs (June 2026,
from the source-research pass). Seeds are ordinary registry rows, not
special-cased code; matching is by unique name, so user edits survive
restarts and re-seeding never duplicates.

Tiers: 1 official/primary · 2 established national media + fact-checkers ·
3 aggregator. Google News is t1_exempt (high volume, heavy syndication —
metadata-only in Phase 1 unless watch-hit).
"""

from __future__ import annotations

import sqlite3

from connect.storage import sources as source_dao

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
)


def seed_sources(conn: sqlite3.Connection) -> int:
    """Insert missing seed rows (matched by name); returns how many added."""
    added = 0
    for seed in SEED_SOURCES:
        if source_dao.get_by_name(conn, seed["name"]) is not None:
            continue
        source_dao.insert(
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
