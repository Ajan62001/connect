"""T1 -> T2 promotion triggers — pure rules first, one DB-reading gatherer.

A document is promoted to T2 (event clustering + story threading) when ANY
trigger fires. ``promotion_triggers`` is the pure, table-testable rule set;
``evaluate`` gathers its inputs from the DB for one document. The returned
list names every trigger that fired (audit-friendly), in stable order.

Triggers (design doc result.pipeline):
- watch_hit:        the T0 watch matcher flagged the document
- fact_checker:     the source is a fact-checker feed (carries verdicts)
- official_tier1:   the source is official/primary (credibility_tier == 1)
- check_worthiness: some extracted claim has check_worthiness >= 0.7
- cluster_heat:     the doc's event cluster reached >= 5 docs in 48h
- manual:           user clicked promote in the UI
"""

from __future__ import annotations

import psycopg

PROMOTE_MIN_CHECK_WORTHINESS = 0.7
CLUSTER_HEAT_MIN_DOCS = 5
CLUSTER_HEAT_WINDOW_HOURS = 48


async def is_fact_checker_source(conn: psycopg.AsyncConnection,
                                 source_id: int | None) -> bool:
    """Fact-checker sources fast-path to T1 and always promote to T2 (their
    items carry verdicts). Marked by config {"fact_checker": true} or a
    'fact-check' note (the seeded Alt News / BOOM / PIB Fact Check rows
    match the latter)."""
    if source_id is None:
        return False
    cur = await conn.execute(
        "SELECT config, notes FROM source WHERE id = %s", (source_id,))
    row = await cur.fetchone()
    if row is None:
        return False
    config = row["config"] if isinstance(row["config"], dict) else {}
    if config.get("fact_checker"):
        return True
    notes = (row["notes"] or "").lower()
    return "fact-check" in notes or "fact check" in notes


def promotion_triggers(*, watch_hit: bool = False,
                       fact_checker: bool = False,
                       official_tier1: bool = False,
                       max_check_worthiness: float | None = None,
                       cluster_docs_48h: int = 0,
                       manual: bool = False) -> list[str]:
    """Pure rule table: which triggers fire for these inputs."""
    fired: list[str] = []
    if watch_hit:
        fired.append("watch_hit")
    if fact_checker:
        fired.append("fact_checker")
    if official_tier1:
        fired.append("official_tier1")
    if (max_check_worthiness is not None
            and max_check_worthiness >= PROMOTE_MIN_CHECK_WORTHINESS):
        fired.append("check_worthiness")
    if cluster_docs_48h >= CLUSTER_HEAT_MIN_DOCS:
        fired.append("cluster_heat")
    if manual:
        fired.append("manual")
    return fired


async def evaluate(conn: psycopg.AsyncConnection, document_id: int, *,
                   manual: bool = False) -> list[str]:
    """Gather inputs for one document and run the rules. Empty list = stay
    at T1. Safe to call for unknown ids (no triggers)."""
    cur = await conn.execute(
        "SELECT d.watch_hit, d.source_id, s.credibility_tier"
        " FROM document d LEFT JOIN source s ON s.id = d.source_id"
        " WHERE d.id = %s", (document_id,))
    doc = await cur.fetchone()
    if doc is None:
        return []
    cur = await conn.execute(
        "SELECT MAX(c.check_worthiness) AS w FROM claim_sighting cs"
        " JOIN claim c ON c.id = cs.claim_id WHERE cs.document_id = %s",
        (document_id,))
    max_worthiness = (await cur.fetchone())["w"]
    return promotion_triggers(
        watch_hit=bool(doc["watch_hit"]),
        fact_checker=await is_fact_checker_source(conn, doc["source_id"]),
        official_tier1=doc["credibility_tier"] == 1,
        max_check_worthiness=max_worthiness,
        cluster_docs_48h=await cluster_heat(conn, document_id),
        manual=manual)


async def cluster_heat(conn: psycopg.AsyncConnection,
                       document_id: int) -> int:
    """Docs assigned to the same event as this doc within the last 48h
    ("story heating up"); 0 when the doc has no event assignment yet."""
    cur = await conn.execute(
        "SELECT event_id FROM event_assignment"
        " WHERE document_id = %s AND event_id IS NOT NULL"
        " ORDER BY id DESC LIMIT 1", (document_id,))
    row = await cur.fetchone()
    if row is None:
        return 0
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT document_id) AS n FROM event_assignment"
        " WHERE event_id = %s AND created_at >= now() - %s::interval",
        (row["event_id"], f"{CLUSTER_HEAT_WINDOW_HOURS} hours"))
    return int((await cur.fetchone())["n"])
