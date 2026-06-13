"""Resolve a StorySeed into a CLOSED StoryFactSet — the only facts the story
may cite. Every quote is verbatim (a finding's already-verified quote, or a
literal document snippet), and every document read is viewer-scoped.

Phase A: story-thread + investigation sources. Phase B adds workspace + topic.
"""

from __future__ import annotations

from typing import Any

import psycopg

from connect.agents.workspace_agent import _build_focus
from connect.retrieval.search import hybrid_document_ids
from connect.story.schema import StoryFact, StoryFactSet, StorySeed
from connect.storage import workspaces as workspace_dao

# bounds that keep the evidence menu (and the synthesis prompt) reasonable
MAX_FACTS = 60
SNIPPET_CHARS = 320
DOCS_PER_EVENT = 3
TOPIC_POOL = 40

_VISIBLE = "(d.visibility = 'shared' OR d.owner_id = %s)"


async def resolve_factset(conn: psycopg.AsyncConnection, seed: StorySeed, *,
                          viewer: int, embedder: Any = None,
                          vectors: Any = None) -> StoryFactSet:
    if seed.investigation_id is not None:
        return await _from_investigation(conn, seed.investigation_id,
                                         viewer=viewer)
    if seed.story_id is not None:
        return await _from_story_thread(conn, seed.story_id, viewer=viewer)
    if seed.workspace_id is not None:
        return await _from_workspace(conn, seed.workspace_id, viewer=viewer)
    if seed.topic:
        return await _from_topic(conn, seed.topic, seed.since, seed.until,
                                 viewer=viewer, embedder=embedder,
                                 vectors=vectors)
    raise LookupError("unsupported story source")


async def _from_investigation(conn: psycopg.AsyncConnection,
                              investigation_id: int, *,
                              viewer: int) -> StoryFactSet:
    """An investigation's findings (each carries a verbatim-verified quote) are
    the fact-set — the strongest grounding source."""
    cur = await conn.execute(
        "SELECT input_text FROM dossier WHERE id = %s AND kind ="
        " 'investigation' AND (owner_id = %s OR visibility = 'shared')",
        (investigation_id, viewer))
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"investigation {investigation_id} not found")
    subject = row["input_text"]

    cur = await conn.execute(
        "SELECT f.id AS finding_id, f.speculation,"
        " fe.document_id, fe.quote,"
        " d.title, d.published_at, s.name AS source_name,"
        " s.credibility_tier"
        " FROM finding f"
        " JOIN finding_evidence fe ON fe.finding_id = f.id"
        " JOIN document d ON d.id = fe.document_id"
        " LEFT JOIN source s ON s.id = d.source_id"
        f" WHERE f.dossier_id = %s AND {_VISIBLE}"
        " ORDER BY f.id, fe.id",
        (investigation_id, viewer))
    facts: list[StoryFact] = []
    seen: set[int] = set()
    for r in await cur.fetchall():
        if r["finding_id"] in seen:        # first evidence quote per finding
            continue
        seen.add(r["finding_id"])
        facts.append(StoryFact(
            document_id=r["document_id"], quote=r["quote"], title=r["title"],
            source_name=r["source_name"],
            credibility_tier=r["credibility_tier"],
            occurred_on=_day(r["published_at"]), finding_id=r["finding_id"]))
        if len(facts) >= MAX_FACTS:
            break
    return StoryFactSet(subject=subject, facts=facts)


async def _from_story_thread(conn: psycopg.AsyncConnection, story_id: int, *,
                             viewer: int) -> StoryFactSet:
    """A KB story thread: its events in time order, each event's documents
    contributing a verbatim snippet."""
    cur = await conn.execute("SELECT title FROM story WHERE id = %s",
                             (story_id,))
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"story {story_id} not found")
    subject = row["title"] or f"Story #{story_id}"

    cur = await conn.execute(
        "SELECT id, title, occurred_on FROM event WHERE story_id = %s"
        " ORDER BY occurred_on NULLS LAST, id", (story_id,))
    events = await cur.fetchall()

    facts: list[StoryFact] = []
    for ev in events:
        dcur = await conn.execute(
            "SELECT d.id, d.title, d.content_text, d.published_at,"
            " s.name AS source_name, s.credibility_tier"
            " FROM event_assignment ea"
            " JOIN document d ON d.id = ea.document_id"
            " LEFT JOIN source s ON s.id = d.source_id"
            f" WHERE ea.event_id = %s AND {_VISIBLE}"
            " ORDER BY d.published_at NULLS LAST, d.id LIMIT %s",
            (ev["id"], viewer, DOCS_PER_EVENT))
        for d in await dcur.fetchall():
            snippet = (d["content_text"] or "").strip()[:SNIPPET_CHARS]
            if not snippet:
                continue
            facts.append(StoryFact(
                document_id=d["id"], quote=snippet, title=d["title"],
                source_name=d["source_name"],
                credibility_tier=d["credibility_tier"],
                occurred_on=_day(ev["occurred_on"])))
            if len(facts) >= MAX_FACTS:
                return StoryFactSet(subject=subject, facts=facts)
    return StoryFactSet(subject=subject, facts=facts)


async def _from_workspace(conn: psycopg.AsyncConnection, workspace_id: int, *,
                          viewer: int) -> StoryFactSet:
    """A workspace lens: its grounded findings (posts with a verbatim-verified
    quote) first, then documents matching its focus."""
    ws = await workspace_dao.get(conn, workspace_id, viewer=viewer)
    if ws is None:
        raise LookupError(f"workspace {workspace_id} not found")
    facts: list[StoryFact] = []

    # 1. grounded posts tagged to the workspace (their quote is verbatim).
    cur = await conn.execute(
        "SELECT p.document_id, p.quote, d.title, d.published_at,"
        " s.name AS source_name, s.credibility_tier"
        " FROM post p JOIN document d ON d.id = p.document_id"
        " LEFT JOIN source s ON s.id = d.source_id"
        " WHERE p.workspace_id = %s AND p.quote IS NOT NULL"
        " AND (p.visibility = 'shared' OR p.owner_id = %s)"
        f" AND {_VISIBLE} ORDER BY p.created_at DESC LIMIT %s",
        (workspace_id, viewer, viewer, MAX_FACTS // 2))
    for r in await cur.fetchall():
        facts.append(StoryFact(
            document_id=r["document_id"], quote=r["quote"], title=r["title"],
            source_name=r["source_name"],
            credibility_tier=r["credibility_tier"],
            occurred_on=_day(r["published_at"])))

    # 2. documents matching the workspace focus (snippets).
    clause, params = _build_focus(ws)
    sql = ("SELECT d.id, d.title, d.content_text, d.published_at,"
           " s.name AS source_name, s.credibility_tier"
           " FROM document d LEFT JOIN source s ON s.id = d.source_id"
           f" WHERE {_VISIBLE}")
    qp: list[Any] = [viewer]
    if clause:
        sql += " AND " + clause
        qp += params
    sql += " ORDER BY d.fetched_at DESC, d.id DESC LIMIT %s"
    qp.append(MAX_FACTS)
    cur = await conn.execute(sql, qp)
    seen_docs = {f.document_id for f in facts}
    for r in await cur.fetchall():
        if r["id"] in seen_docs:
            continue
        snippet = (r["content_text"] or "").strip()[:SNIPPET_CHARS]
        if not snippet:
            continue
        facts.append(StoryFact(
            document_id=r["id"], quote=snippet, title=r["title"],
            source_name=r["source_name"],
            credibility_tier=r["credibility_tier"],
            occurred_on=_day(r["published_at"])))
        if len(facts) >= MAX_FACTS:
            break
    return StoryFactSet(subject=ws.name, facts=facts)


async def _from_topic(conn: psycopg.AsyncConnection, topic: str,
                      since: str | None, until: str | None, *, viewer: int,
                      embedder: Any, vectors: Any) -> StoryFactSet:
    """A free-form topic: lightweight hybrid retrieval over the corpus (NOT a
    full investigation), each retrieved document contributing a snippet."""
    ids = await hybrid_document_ids(
        conn, topic, embedder=embedder, vectors=vectors,
        lexical_k=TOPIC_POOL, vector_k=TOPIC_POOL, viewer=viewer)
    facts: list[StoryFact] = []
    for doc_id in ids:
        cur = await conn.execute(
            "SELECT d.id, d.title, d.content_text, d.published_at,"
            " s.name AS source_name, s.credibility_tier"
            " FROM document d LEFT JOIN source s ON s.id = d.source_id"
            f" WHERE d.id = %s AND {_VISIBLE}", (doc_id, viewer))
        r = await cur.fetchone()
        if r is None:
            continue
        day = _day(r["published_at"])
        if since and day and day < str(since)[:10]:
            continue
        if until and day and day > str(until)[:10]:
            continue
        snippet = (r["content_text"] or "").strip()[:SNIPPET_CHARS]
        if not snippet:
            continue
        facts.append(StoryFact(
            document_id=r["id"], quote=snippet, title=r["title"],
            source_name=r["source_name"],
            credibility_tier=r["credibility_tier"], occurred_on=day))
        if len(facts) >= MAX_FACTS:
            break
    return StoryFactSet(subject=topic, facts=facts)


def _day(value: object) -> str | None:
    """ISO day string from a timestamptz/date/str column value."""
    if value is None:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else text
