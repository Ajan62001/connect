"""Story threading — follows edges + union-find story maintenance.

On NEW-event creation: search prior events (<= 90 days back) sharing >= 2
entities whose event_type is lifecycle-related (same non-null
lifecycle_group, or the same event_type for ungrouped types) -> one FAST
yes/no "is E_new a follow-up development of E_old?" per candidate (top 3,
stop at the first yes) -> edge(E_new -follows-> E_old) + story union-find
merge.

A story row is the materialized connected component of follows edges —
created lazily when the first follows edge lands, merged when an edge
bridges two stories. The graph edges remain the source of truth; the story
row exists purely for cheap thread listing. story.title = root (earliest)
event's title until the user renames; doc_count = sum of member events'.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from datetime import timedelta
from typing import Any, Mapping

import psycopg
from pydantic import BaseModel, ConfigDict

from connect.knowledge.linking import event_clusterer
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.storage import edges as edge_dao
from connect.storage.pg import utc_now

log = logging.getLogger(__name__)

_Row = Mapping[str, Any]

FOLLOW_WINDOW_DAYS = 90
MIN_SHARED_ENTITIES = 2
MAX_FOLLOW_CANDIDATES = 3

EST_FOLLOWUP_INPUT_TOKENS = 600
EST_FOLLOWUP_OUTPUT_TOKENS = 50
PURPOSE_FOLLOWUP = "story_followup"
FOLLOWUP_PROMPT_VERSION = "story-follow-v1"

FOLLOWUP_SYSTEM = """You judge whether one news event is a FOLLOW-UP \
development of an earlier event in the same evolving story (e.g. a bill \
moving to its next stage, a ruling on a previously reported case, a \
scheme's rollout after its announcement). Same actors covering the same \
underlying matter at a LATER stage = follow-up. Merely similar topics or \
shared actors on unrelated matters = not a follow-up. Be conservative."""

_EVENT_DAY = "COALESCE(e.occurred_on, (e.created_at AT TIME ZONE 'utc')::date)"


class FollowUpJudgment(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_follow_up: bool


async def find_follow_candidates(conn: psycopg.AsyncConnection,
                                 event_id: int) -> list[_Row]:
    """Prior events within the window whose event_type is lifecycle-related,
    sharing >= MIN_SHARED_ENTITIES entities; most-shared first."""
    cur = await conn.execute(
        f"SELECT e.id, e.event_type, et.lifecycle_group,"
        f" {_EVENT_DAY} AS day"
        f" FROM event e LEFT JOIN event_type et ON et.name = e.event_type"
        f" WHERE e.id = %s", (event_id,))
    new = await cur.fetchone()
    if new is None:
        return []
    new_entities = await event_clusterer.event_entity_ids(conn, event_id)
    if len(new_entities) < MIN_SHARED_ENTITIES:
        return []
    window_start = (_date.fromisoformat(new["day"])
                    - timedelta(days=FOLLOW_WINDOW_DAYS)).isoformat()
    cur = await conn.execute(
        f"SELECT e.id, e.title, e.description, e.event_type, e.occurred_on,"
        f" et.lifecycle_group,"
        f" {_EVENT_DAY} AS day"
        f" FROM event e LEFT JOIN event_type et ON et.name = e.event_type"
        f" WHERE e.id != %s AND {_EVENT_DAY} <= %s::date"
        f" AND {_EVENT_DAY} >= %s::date",
        (event_id, new["day"], window_start))
    rows = await cur.fetchall()
    scored: list[tuple[int, _Row]] = []
    for row in rows:
        related = (
            (row["lifecycle_group"] is not None
             and row["lifecycle_group"] == new["lifecycle_group"])
            or row["event_type"] == new["event_type"])
        if not related:
            continue
        shared = len(new_entities
                     & await event_clusterer.event_entity_ids(conn,
                                                              row["id"]))
        if shared >= MIN_SHARED_ENTITIES:
            scored.append((shared, row))
    scored.sort(key=lambda t: (-t[0], t[1]["id"]))
    return [row for _, row in scored[:MAX_FOLLOW_CANDIDATES]]


async def thread_new_event(conn: psycopg.AsyncConnection,
                           provider: LLMProvider | None,
                           governor: Governor, event_id: int,
                           provenance_document_id: int | None) -> dict:
    """Run the follow-up search for a freshly created event. Returns
    {candidates, followed_event_id, story_id}. Degrades to no threading
    without a provider or budget — never raises into T2."""
    candidates = await find_follow_candidates(conn, event_id)
    stats: dict = {"candidates": len(candidates), "followed_event_id": None,
                   "story_id": None}
    if not candidates or provider is None:
        return stats
    cur = await conn.execute(
        "SELECT title, description, event_type, occurred_on FROM event"
        " WHERE id = %s", (event_id,))
    new = await cur.fetchone()
    model = provider.model_for(ModelTier.FAST)
    for cand in candidates:
        try:
            await governor.check(spend.cost_usd(
                model, input_tokens=EST_FOLLOWUP_INPUT_TOKENS,
                output_tokens=EST_FOLLOWUP_OUTPUT_TOKENS))
        except BudgetExceeded as e:
            log.warning("follow-up judgment skipped (event %s): %s",
                        event_id, e)
            break
        try:
            completion = await provider.complete_structured(
                system=FOLLOWUP_SYSTEM,
                messages=[{"role": "user",
                           "content": _followup_message(new, cand)}],
                schema=FollowUpJudgment, tier=ModelTier.FAST, max_tokens=64)
        except LLMError as e:
            log.warning("follow-up judgment failed (event %s vs %s): %s",
                        event_id, cand["id"], e)
            continue
        await spend.record_call(conn, purpose=PURPOSE_FOLLOWUP,
                                model=completion.model,
                                usage=completion.usage)
        if completion.output.is_follow_up:
            await edge_dao.insert(
                conn, src_type="event", src_id=event_id,
                dst_type="event", dst_id=cand["id"], relation="follows",
                provenance_document_id=provenance_document_id, grade=1)
            stats["followed_event_id"] = cand["id"]
            stats["story_id"] = await merge_into_story(conn, event_id,
                                                       cand["id"])
            break
    return stats


def _followup_message(new: _Row, old: _Row) -> str:
    return (
        "EARLIER EVENT:\n"
        f"  [{old['event_type']}] {old['title']}"
        f" ({old['occurred_on'] or 'undated'})\n"
        f"  {old['description'] or ''}\n\n"
        "NEW EVENT:\n"
        f"  [{new['event_type']}] {new['title']}"
        f" ({new['occurred_on'] or 'undated'})\n"
        f"  {new['description'] or ''}\n\n"
        "Is the new event a follow-up development of the earlier event?")


# -- union-find story maintenance ---------------------------------------------------

async def merge_into_story(conn: psycopg.AsyncConnection, event_a: int,
                           event_b: int) -> int:
    """Union the two events' story components; returns the surviving story
    id. Cases: neither has a story (create), one has (adopt), both have
    different ones (merge into the lower id — the older story)."""
    cur = await conn.execute(
        "SELECT id, story_id FROM event WHERE id IN (%s, %s)"
        " ORDER BY (id <> %s)",
        (event_a, event_b, event_a))
    story_a, story_b = await cur.fetchall()
    sid_a, sid_b = story_a["story_id"], story_b["story_id"]

    if sid_a is None and sid_b is None:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO story (status, doc_count, created_at)"
                " VALUES ('active', 0, %s) RETURNING id", (utc_now(),))
            story_id = int((await cur.fetchone())["id"])
            await conn.execute(
                "UPDATE event SET story_id = %s WHERE id IN (%s, %s)",
                (story_id, event_a, event_b))
    elif sid_a is not None and sid_b is not None and sid_a != sid_b:
        story_id, absorbed = sorted((sid_a, sid_b))
        async with conn.transaction():
            await conn.execute(
                "UPDATE event SET story_id = %s WHERE story_id = %s",
                (story_id, absorbed))
            await conn.execute(
                "DELETE FROM story WHERE id = %s", (absorbed,))
    else:
        story_id = sid_a if sid_a is not None else sid_b  # type: ignore[assignment]
        async with conn.transaction():
            await conn.execute(
                "UPDATE event SET story_id = %s WHERE id IN (%s, %s)",
                (story_id, event_a, event_b))
    await refresh_story(conn, story_id)
    return story_id


async def refresh_story(conn: psycopg.AsyncConnection,
                        story_id: int) -> None:
    """Recompute the materialized story row from its member events: root =
    earliest event, title = root title (only until the user renames — a
    user-set title is grade-3 curation and is preserved once root is set
    and unchanged), doc_count = sum, updated_at = now."""
    cur = await conn.execute(
        "SELECT id, title FROM event WHERE story_id = %s"
        " ORDER BY COALESCE(occurred_on,"
        "   (created_at AT TIME ZONE 'utc')::date) ASC, id ASC LIMIT 1",
        (story_id,))
    root = await cur.fetchone()
    if root is None:
        return
    cur = await conn.execute(
        "SELECT COALESCE(SUM(doc_count), 0) AS n FROM event"
        " WHERE story_id = %s",
        (story_id,))
    doc_count = (await cur.fetchone())["n"]
    async with conn.transaction():
        await conn.execute(
            "UPDATE story SET root_event_id = %s,"
            " title = CASE WHEN title IS NULL OR root_event_id IS NULL"
            "   OR root_event_id != %s THEN %s ELSE title END,"
            " doc_count = %s, updated_at = %s WHERE id = %s",
            (root["id"], root["id"], root["title"], doc_count, utc_now(),
             story_id))
