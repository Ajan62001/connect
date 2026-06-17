"""Resolve a ContentSeed into a CLOSED StoryFactSet — the only facts the
campaign may cite. Four of the five sources delegate to story mode's resolver
(identical fact-set currency); ``story_dossier_id`` repurposes an existing
story-mode narrative by reading back its persisted, already-verified facts.
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.content.schema import ContentSeed
from connect.story.factset import resolve_factset as resolve_story_factset
from connect.story.schema import StoryFact, StoryFactSet, StorySeed


async def resolve_content_factset(conn: psycopg.AsyncConnection,
                                  seed: ContentSeed, *, viewer: int,
                                  embedder: Any = None,
                                  vectors: Any = None) -> StoryFactSet:
    if seed.story_dossier_id is not None:
        return await _from_story_dossier(conn, seed.story_dossier_id,
                                         viewer=viewer)
    # the other four sources are exactly story mode's — reuse its resolver.
    story_seed = StorySeed(
        story_id=seed.story_id, investigation_id=seed.investigation_id,
        workspace_id=seed.workspace_id, topic=seed.topic,
        since=seed.since, until=seed.until)
    return await resolve_story_factset(conn, story_seed, viewer=viewer,
                                       embedder=embedder, vectors=vectors)


async def _from_story_dossier(conn: psycopg.AsyncConnection,
                              dossier_id: int, *,
                              viewer: int) -> StoryFactSet:
    """Repurpose a story-mode narrative: its 'scope' section persisted the
    exact closed StoryFactSet it was synthesized from (verbatim quotes +
    credibility tiers), so reading it back gives the identical menu."""
    cur = await conn.execute(
        "SELECT title, input_text FROM dossier WHERE id = %s AND kind ="
        " 'story' AND (owner_id = %s OR visibility = 'shared')",
        (dossier_id, viewer))
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"story {dossier_id} not found")
    subject = row["title"] or row["input_text"] or f"Story #{dossier_id}"

    cur = await conn.execute(
        "SELECT content FROM dossier_section WHERE dossier_id = %s"
        " AND stage = 'scope'", (dossier_id,))
    sec = await cur.fetchone()
    content: dict[str, Any] = (sec["content"] if sec
                               and isinstance(sec["content"], dict) else {})
    facts_raw = content.get("facts") or []
    facts: list[StoryFact] = []
    for f in facts_raw:
        if isinstance(f, dict) and (f.get("quote") or "").strip():
            facts.append(StoryFact(**{k: f.get(k) for k in (
                "document_id", "quote", "title", "source_name",
                "credibility_tier", "occurred_on", "finding_id")}))
    if not facts:
        raise LookupError(
            f"story {dossier_id} has no gathered facts to repurpose")
    return StoryFactSet(subject=subject, facts=facts)
