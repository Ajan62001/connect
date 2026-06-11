"""Closed event-type taxonomy — seeded fixture (idempotent).

(name, lifecycle_group, window_days). The window drives event-clustering
candidate retrieval in Phase 2; the lifecycle_group powers `follows` /
story threading. India-domain pass on the names is a flagged open item.
"""

from __future__ import annotations

import psycopg

EVENT_TYPES: tuple[tuple[str, str | None, int], ...] = (
    ("cabinet_decision", "executive", 7),
    ("scheme_announcement", "scheme", 14),
    ("bill_stage", "legislative", 30),
    ("ordinance", "legislative", 21),
    ("gazette_notification", "legislative", 14),
    ("court_ruling", "judicial", 14),
    ("rbi_action", "monetary", 3),
    ("sebi_action", "regulatory", 7),
    ("budget_event", "fiscal", 21),
    ("gst_council_decision", "fiscal", 14),
    ("election_event", "election", 14),
    ("factcheck", None, 7),
    ("appointment", "executive", 7),
    ("mou_signing", None, 14),
    ("data_release", None, 7),
    ("statement", None, 5),
    ("other", None, 7),
)

EVENT_TYPE_NAMES: tuple[str, ...] = tuple(name for name, _, _ in EVENT_TYPES)


async def seed_event_types(conn: psycopg.AsyncConnection) -> int:
    """Insert missing taxonomy rows; returns how many were added."""
    added = 0
    async with conn.transaction():
        for name, group, window in EVENT_TYPES:
            cur = await conn.execute(
                "INSERT INTO event_type (name, lifecycle_group, window_days)"
                " VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                (name, group, window))
            added += cur.rowcount
    return added
