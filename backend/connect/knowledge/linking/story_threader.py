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
import sqlite3

from pydantic import BaseModel, ConfigDict

from connect.knowledge.linking import event_clusterer
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.storage import edges as edge_dao
from connect.storage.db import utc_now

log = logging.getLogger(__name__)

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


class FollowUpJudgment(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_follow_up: bool


def find_follow_candidates(conn: sqlite3.Connection,
                           event_id: int) -> list[sqlite3.Row]:
    """Prior events within the window whose event_type is lifecycle-related,
    sharing >= MIN_SHARED_ENTITIES entities; most-shared first."""
    new = conn.execute(
        "SELECT e.id, e.event_type, et.lifecycle_group,"
        " COALESCE(e.occurred_on, substr(e.created_at, 1, 10)) AS day"
        " FROM event e LEFT JOIN event_type et ON et.name = e.event_type"
        " WHERE e.id = ?", (event_id,)).fetchone()
    if new is None:
        return []
    new_entities = event_clusterer.event_entity_ids(conn, event_id)
    if len(new_entities) < MIN_SHARED_ENTITIES:
        return []
    rows = conn.execute(
        "SELECT e.id, e.title, e.description, e.event_type, e.occurred_on,"
        " et.lifecycle_group,"
        " COALESCE(e.occurred_on, substr(e.created_at, 1, 10)) AS day"
        " FROM event e LEFT JOIN event_type et ON et.name = e.event_type"
        " WHERE e.id != ? AND day <= ? AND day >= date(?, ?)",
        (event_id, new["day"], new["day"],
         f"-{FOLLOW_WINDOW_DAYS} days")).fetchall()
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        related = (
            (row["lifecycle_group"] is not None
             and row["lifecycle_group"] == new["lifecycle_group"])
            or row["event_type"] == new["event_type"])
        if not related:
            continue
        shared = len(new_entities
                     & event_clusterer.event_entity_ids(conn, row["id"]))
        if shared >= MIN_SHARED_ENTITIES:
            scored.append((shared, row))
    scored.sort(key=lambda t: (-t[0], t[1]["id"]))
    return [row for _, row in scored[:MAX_FOLLOW_CANDIDATES]]


async def thread_new_event(conn: sqlite3.Connection,
                           provider: LLMProvider | None,
                           governor: Governor, event_id: int,
                           provenance_document_id: int | None) -> dict:
    """Run the follow-up search for a freshly created event. Returns
    {candidates, followed_event_id, story_id}. Degrades to no threading
    without a provider or budget — never raises into T2."""
    candidates = find_follow_candidates(conn, event_id)
    stats: dict = {"candidates": len(candidates), "followed_event_id": None,
                   "story_id": None}
    if not candidates or provider is None:
        return stats
    new = conn.execute(
        "SELECT title, description, event_type, occurred_on FROM event"
        " WHERE id = ?", (event_id,)).fetchone()
    model = provider.model_for(ModelTier.FAST)
    for cand in candidates:
        try:
            governor.check(spend.cost_usd(
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
        spend.record_call(conn, purpose=PURPOSE_FOLLOWUP,
                          model=completion.model, usage=completion.usage)
        if completion.output.is_follow_up:
            edge_dao.insert(
                conn, src_type="event", src_id=event_id,
                dst_type="event", dst_id=cand["id"], relation="follows",
                provenance_document_id=provenance_document_id, grade=1)
            stats["followed_event_id"] = cand["id"]
            stats["story_id"] = merge_into_story(conn, event_id, cand["id"])
            break
    return stats


def _followup_message(new: sqlite3.Row, old: sqlite3.Row) -> str:
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

def merge_into_story(conn: sqlite3.Connection, event_a: int,
                     event_b: int) -> int:
    """Union the two events' story components; returns the surviving story
    id. Cases: neither has a story (create), one has (adopt), both have
    different ones (merge into the lower id — the older story)."""
    story_a, story_b = (conn.execute(
        "SELECT id, story_id FROM event WHERE id IN (?, ?) ORDER BY id != ?",
        (event_a, event_b, event_a)).fetchall())
    sid_a, sid_b = story_a["story_id"], story_b["story_id"]

    if sid_a is None and sid_b is None:
        with conn:
            cur = conn.execute(
                "INSERT INTO story (status, doc_count, created_at)"
                " VALUES ('active', 0, ?)", (utc_now(),))
            story_id = int(cur.lastrowid)  # type: ignore[arg-type]
            conn.execute("UPDATE event SET story_id = ? WHERE id IN (?, ?)",
                         (story_id, event_a, event_b))
    elif sid_a is not None and sid_b is not None and sid_a != sid_b:
        story_id, absorbed = sorted((sid_a, sid_b))
        with conn:
            conn.execute("UPDATE event SET story_id = ? WHERE story_id = ?",
                         (story_id, absorbed))
            conn.execute("DELETE FROM story WHERE id = ?", (absorbed,))
    else:
        story_id = sid_a if sid_a is not None else sid_b  # type: ignore[assignment]
        with conn:
            conn.execute(
                "UPDATE event SET story_id = ? WHERE id IN (?, ?)",
                (story_id, event_a, event_b))
    refresh_story(conn, story_id)
    return story_id


def refresh_story(conn: sqlite3.Connection, story_id: int) -> None:
    """Recompute the materialized story row from its member events: root =
    earliest event, title = root title (only until the user renames — a
    user-set title is grade-3 curation and is preserved once root is set
    and unchanged), doc_count = sum, updated_at = now."""
    root = conn.execute(
        "SELECT id, title FROM event WHERE story_id = ?"
        " ORDER BY COALESCE(occurred_on, substr(created_at, 1, 10)) ASC,"
        " id ASC LIMIT 1", (story_id,)).fetchone()
    if root is None:
        return
    doc_count = conn.execute(
        "SELECT COALESCE(SUM(doc_count), 0) FROM event WHERE story_id = ?",
        (story_id,)).fetchone()[0]
    with conn:
        conn.execute(
            "UPDATE story SET root_event_id = ?,"
            " title = CASE WHEN title IS NULL OR root_event_id IS NULL"
            "   OR root_event_id != ? THEN ? ELSE title END,"
            " doc_count = ?, updated_at = ? WHERE id = ?",
            (root["id"], root["id"], root["title"], doc_count, utc_now(),
             story_id))
