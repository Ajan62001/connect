"""Persist one T1 result — GROUNDING DISCIPLINE STARTS HERE.

- quoted_span must be a verbatim substring of the document's content_text
  (whitespace-normalized comparison, the shared analysis/grounding.py
  verifier); claims that fail the check are DROPPED and counted — an
  invented quote never becomes knowledge.
- entities are get-or-create by normalized alias-exact match (lowercase,
  collapsed whitespace) over entity.name + aliases; a novel surface form is
  appended to the matched entity's aliases (the alias table grows, which
  makes the free T0 watch-matcher better over time).
- topics are filtered to the controlled vocabulary; event_type to the seeded
  taxonomy ('other' fallback).
- claims with check_worthiness >= CLAIM_MIN_WORTHINESS become claim +
  claim_sighting rows (grade 1, extractor_model).
- Idempotent: re-enriching a document deletes its prior T1 rows first; the
  whole replace runs in ONE transaction (raw SQL — the per-statement DAO
  helpers each own a transaction; psycopg nests them as SAVEPOINTs, but
  this module keeps the explicit one-transaction shape).
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.analysis.grounding import find_span as _find_span
from connect.analysis.grounding import norm_ws as _norm_ws
from connect.analysis.grounding import span_is_verbatim  # noqa: F401 — re-export
from connect.domain.enums import (
    ENTITY_TYPES,
    STATEMENT_SPEAKER_TYPES,
    T1_TOPICS,
)
from connect.knowledge.enrichment.prompts import T1_PROMPT_VERSION
from connect.knowledge.enrichment.t1 import EnrichmentT1
from connect.knowledge.taxonomy import EVENT_TYPE_NAMES
from connect.storage.pg import Jsonb, utc_now

CLAIM_MIN_WORTHINESS = 0.6

_Row = Mapping[str, Any]


def _norm_alias(text: str) -> str:
    """Entity-matching normal form: lowercase + collapsed whitespace."""
    return _norm_ws(text).lower()


async def _entity_index(conn: psycopg.AsyncConnection) -> dict[str, _Row]:
    """normalized name/alias -> entity row (single-user corpus scale)."""
    index: dict[str, _Row] = {}
    cur = await conn.execute(
        "SELECT id, name, entity_type, aliases FROM entity")
    for row in await cur.fetchall():
        index.setdefault(_norm_alias(row["name"]), row)
        for alias in (row["aliases"] or []):
            if isinstance(alias, str):
                index.setdefault(_norm_alias(alias), row)
    return index


async def persist_t1(conn: psycopg.AsyncConnection, *, document_id: int,
                     result: EnrichmentT1, model: str,
                     prompt_version: str = T1_PROMPT_VERSION,
                     ) -> dict[str, Any]:
    """Replace the document's T1 rows with ``result``; one transaction.

    Returns counters: topics, entities_created, entities_matched, mentions,
    claims, claims_dropped_span, claims_below_threshold, statements,
    statements_dropped_span, statements_dropped_speaker.
    """
    cur = await conn.execute(
        "SELECT id, content_text, published_at, fetched_at FROM document"
        " WHERE id = %s", (document_id,))
    doc = await cur.fetchone()
    if doc is None:
        raise ValueError(f"document {document_id} not found")
    content_text = doc["content_text"] or ""
    stated_at = doc["published_at"] or doc["fetched_at"]
    now = utc_now()

    stats = {"topics": 0, "entities_created": 0, "entities_matched": 0,
             "mentions": 0, "claims": 0, "claims_dropped_span": 0,
             "claims_below_threshold": 0, "statements": 0,
             "statements_dropped_span": 0, "statements_dropped_speaker": 0}

    entity_index = await _entity_index(conn)

    async with conn.transaction():
        # --- delete-then-insert: replace this document's T1 rows ------------
        await conn.execute(
            "DELETE FROM document_enrichment WHERE document_id = %s",
            (document_id,))
        await conn.execute(
            "DELETE FROM document_topic WHERE document_id = %s"
            " AND source = 't1'",
            (document_id,))
        await conn.execute(
            "DELETE FROM entity_mention WHERE document_id = %s",
            (document_id,))
        await conn.execute(
            "DELETE FROM claim_sighting WHERE document_id = %s",
            (document_id,))
        await conn.execute(
            "DELETE FROM statement WHERE document_id = %s", (document_id,))
        # claims first sighted here and now orphaned go too
        await conn.execute(
            "DELETE FROM claim WHERE first_document_id = %s AND NOT EXISTS"
            " (SELECT 1 FROM claim_sighting WHERE claim_id = claim.id)",
            (document_id,))

        # --- enrichment row ---------------------------------------------------
        event_type = (result.event_type
                      if result.event_type in EVENT_TYPE_NAMES else "other")
        await conn.execute(
            "INSERT INTO document_enrichment (document_id, summary,"
            " event_type, model, prompt_version, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (document_id, result.summary, event_type, model,
             prompt_version, now))

        # --- topics (controlled vocabulary only) ----------------------------
        for topic in result.topics:
            if topic not in T1_TOPICS:
                continue
            cur = await conn.execute(
                "INSERT INTO document_topic (document_id, topic, source)"
                " VALUES (%s,%s, 't1')"
                " ON CONFLICT (document_id, topic) DO NOTHING",
                (document_id, topic))
            stats["topics"] += cur.rowcount

        # --- entities: get-or-create by normalized alias-exact match --------
        seen_norms: set[str] = set()
        for ent in result.entities:
            surface = _norm_ws(ent.surface)
            if not surface:
                continue
            norm = _norm_alias(surface)
            if norm in seen_norms:
                continue  # same entity twice in one result
            seen_norms.add(norm)

            row = entity_index.get(norm)
            if row is None:
                entity_type = ent.type if ent.type in ENTITY_TYPES else "other"
                cur = await conn.execute(
                    "INSERT INTO entity (name, entity_type, aliases, grade,"
                    " extractor_model, prompt_version, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (name, entity_type) DO NOTHING"
                    " RETURNING id",
                    (surface, entity_type, Jsonb([surface]), 1,
                     model, prompt_version, now))
                new_row = await cur.fetchone()
                if new_row is not None:
                    entity_id = int(new_row["id"])
                    stats["entities_created"] += 1
                else:  # UNIQUE(name, entity_type) race with identical name
                    cur = await conn.execute(
                        "SELECT id FROM entity WHERE name = %s AND"
                        " entity_type = %s", (surface, entity_type))
                    entity_id = (await cur.fetchone())["id"]
                    stats["entities_matched"] += 1
                cur = await conn.execute(
                    "SELECT id, name, entity_type, aliases FROM entity"
                    " WHERE id = %s", (entity_id,))
                row = await cur.fetchone()
                entity_index[norm] = row
            else:
                entity_id = row["id"]
                stats["entities_matched"] += 1
                # novel surface form (exact string not yet recorded) ->
                # append to aliases; matching stays normalized, growth is
                # verbatim so the table keeps real-world spellings
                aliases = list(row["aliases"] or [])
                if surface != row["name"] and surface not in aliases:
                    aliases.append(surface)
                    await conn.execute(
                        "UPDATE entity SET aliases = %s, updated_at = %s"
                        " WHERE id = %s",
                        (Jsonb(aliases), now, entity_id))
                    cur = await conn.execute(
                        "SELECT id, name, entity_type, aliases FROM entity"
                        " WHERE id = %s", (entity_id,))
                    row = await cur.fetchone()
                    entity_index[norm] = row

            span_start, span_end = _find_span(surface, content_text)
            await conn.execute(
                "INSERT INTO entity_mention (document_id, entity_id,"
                " surface, span_start, span_end, method, grade,"
                " extractor_model, prompt_version, created_at)"
                " VALUES (%s,%s,%s,%s,%s, 'llm', 1, %s, %s, %s)",
                (document_id, entity_id, surface, span_start, span_end,
                 model, prompt_version, now))
            stats["mentions"] += 1

        # --- claims: grounded + worth keeping --------------------------------
        for claim in result.claims:
            if not span_is_verbatim(claim.quoted_span, content_text):
                stats["claims_dropped_span"] += 1
                continue
            if claim.check_worthiness < CLAIM_MIN_WORTHINESS:
                stats["claims_below_threshold"] += 1
                continue
            cur = await conn.execute(
                "INSERT INTO claim (text, first_document_id,"
                " check_worthiness, created_at) VALUES (%s,%s,%s,%s)"
                " RETURNING id",
                (claim.text, document_id, claim.check_worthiness, now))
            claim_id = int((await cur.fetchone())["id"])
            q_start, q_end = _find_span(claim.quoted_span, content_text)
            await conn.execute(
                "INSERT INTO claim_sighting (claim_id, document_id, quote,"
                " quote_start, quote_end, stance, grade, extractor_model,"
                " prompt_version, created_at)"
                " VALUES (%s,%s,%s,%s,%s, 'asserts', 1, %s, %s, %s)",
                (claim_id, document_id, claim.quoted_span, q_start, q_end,
                 model, prompt_version, now))
            stats["claims"] += 1

        # --- statements: grounded + attributable speaker ----------------------
        # The speaker resolves through the SAME normalized alias-exact index
        # the entities loop maintains (speakers are also listed in entities,
        # so this-document speakers are already in the index via the
        # get-or-create path above). Statements whose quote fails span
        # verification, whose speaker is unknown, or whose speaker's entity
        # type cannot speak (not in STATEMENT_SPEAKER_TYPES) are DROPPED and
        # counted — an unattributable or invented quote never becomes a view.
        for st in result.statements:
            if not span_is_verbatim(st.quote, content_text):
                stats["statements_dropped_span"] += 1
                continue
            speaker_norm = _norm_alias(st.speaker_surface)
            row = entity_index.get(speaker_norm)
            if row is None or row["entity_type"] not in STATEMENT_SPEAKER_TYPES:
                stats["statements_dropped_speaker"] += 1
                continue
            topics = [t for t in st.topics if t in T1_TOPICS]
            q_start, q_end = _find_span(st.quote, content_text)
            await conn.execute(
                "INSERT INTO statement (document_id, entity_id, quote,"
                " quote_start, quote_end, topics, position_summary,"
                " stated_at, grade, extractor_model, prompt_version,"
                " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, 1, %s, %s, %s)",
                (document_id, row["id"], st.quote, q_start, q_end,
                 Jsonb(topics), st.position_summary or None,
                 stated_at, model, prompt_version, now))
            stats["statements"] += 1

        # --- flip the document -------------------------------------------------
        await conn.execute(
            "UPDATE document SET enrichment_tier = 1,"
            " enrichment_status = 'done' WHERE id = %s", (document_id,))

    return stats


async def mark_failed(conn: psycopg.AsyncConnection,
                      document_id: int) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document SET enrichment_status = 'failed'"
            " WHERE id = %s",
            (document_id,))


async def mark_queued(conn: psycopg.AsyncConnection,
                      document_ids: list[int]) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document SET enrichment_status = 'queued'"
            " WHERE id = ANY(%s)", (document_ids,))


async def mark_pending(conn: psycopg.AsyncConnection,
                       document_ids: list[int]) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE document SET enrichment_status = 'pending'"
            " WHERE id = ANY(%s)", (document_ids,))
