"""Persist one T1 result — GROUNDING DISCIPLINE STARTS HERE.

- quoted_span must be a verbatim substring of the document's content_text
  (whitespace-normalized comparison); claims that fail the check are DROPPED
  and counted — an invented quote never becomes knowledge.
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
  helpers each own a transaction and would commit mid-way).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from connect.domain.enums import ENTITY_TYPES, T1_TOPICS
from connect.knowledge.enrichment.prompts import T1_PROMPT_VERSION
from connect.knowledge.enrichment.t1 import EnrichmentT1
from connect.knowledge.taxonomy import EVENT_TYPE_NAMES
from connect.storage.db import utc_now

CLAIM_MIN_WORTHINESS = 0.6

_WS_RE = re.compile(r"\s+")


def _norm_ws(text: str) -> str:
    """Whitespace-normalize (the verbatim-span comparison form)."""
    return _WS_RE.sub(" ", text).strip()


def _norm_alias(text: str) -> str:
    """Entity-matching normal form: lowercase + collapsed whitespace."""
    return _norm_ws(text).lower()


def span_is_verbatim(span: str, content_text: str) -> bool:
    """Is ``span`` a verbatim substring of the document text, modulo
    whitespace? Empty spans never ground a claim."""
    span_n = _norm_ws(span)
    return bool(span_n) and span_n in _norm_ws(content_text)


def _find_span(needle: str, haystack: str) -> tuple[int | None, int | None]:
    """Char offsets of the first case-insensitive occurrence (None if the
    surface only matches after whitespace normalization)."""
    idx = haystack.lower().find(needle.lower())
    if idx < 0:
        return None, None
    return idx, idx + len(needle)


def _entity_index(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """normalized name/alias -> entity row (single-user corpus scale)."""
    index: dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT id, name, entity_type, aliases FROM entity"):
        index.setdefault(_norm_alias(row["name"]), row)
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except (ValueError, TypeError):
            aliases = []
        for alias in aliases:
            if isinstance(alias, str):
                index.setdefault(_norm_alias(alias), row)
    return index


def persist_t1(conn: sqlite3.Connection, *, document_id: int,
               result: EnrichmentT1, model: str,
               prompt_version: str = T1_PROMPT_VERSION) -> dict[str, Any]:
    """Replace the document's T1 rows with ``result``; one transaction.

    Returns counters: topics, entities_created, entities_matched, mentions,
    claims, claims_dropped_span, claims_below_threshold.
    """
    doc = conn.execute(
        "SELECT id, content_text FROM document WHERE id = ?",
        (document_id,)).fetchone()
    if doc is None:
        raise ValueError(f"document {document_id} not found")
    content_text = doc["content_text"] or ""
    now = utc_now()

    stats = {"topics": 0, "entities_created": 0, "entities_matched": 0,
             "mentions": 0, "claims": 0, "claims_dropped_span": 0,
             "claims_below_threshold": 0}

    entity_index = _entity_index(conn)

    with conn:
        # --- delete-then-insert: replace this document's T1 rows ------------
        conn.execute(
            "DELETE FROM document_enrichment WHERE document_id = ?",
            (document_id,))
        conn.execute(
            "DELETE FROM document_topic WHERE document_id = ? AND source = 't1'",
            (document_id,))
        conn.execute(
            "DELETE FROM entity_mention WHERE document_id = ?", (document_id,))
        conn.execute(
            "DELETE FROM claim_sighting WHERE document_id = ?", (document_id,))
        # claims first sighted here and now orphaned go too
        conn.execute(
            "DELETE FROM claim WHERE first_document_id = ? AND NOT EXISTS"
            " (SELECT 1 FROM claim_sighting WHERE claim_id = claim.id)",
            (document_id,))

        # --- enrichment row ---------------------------------------------------
        event_type = (result.event_type
                      if result.event_type in EVENT_TYPE_NAMES else "other")
        conn.execute(
            "INSERT INTO document_enrichment (document_id, summary,"
            " event_type, model, prompt_version, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (document_id, result.summary, event_type, model,
             prompt_version, now))

        # --- topics (controlled vocabulary only) ----------------------------
        for topic in result.topics:
            if topic not in T1_TOPICS:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO document_topic (document_id, topic,"
                " source) VALUES (?,?, 't1')", (document_id, topic))
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
                cur = conn.execute(
                    "INSERT OR IGNORE INTO entity (name, entity_type,"
                    " aliases, grade, extractor_model, prompt_version,"
                    " created_at) VALUES (?,?,?,?,?,?,?)",
                    (surface, entity_type, json.dumps([surface]), 1,
                     model, prompt_version, now))
                if cur.rowcount:
                    entity_id = int(cur.lastrowid)  # type: ignore[arg-type]
                    stats["entities_created"] += 1
                else:  # UNIQUE(name, entity_type) race with identical name
                    entity_id = conn.execute(
                        "SELECT id FROM entity WHERE name = ? AND"
                        " entity_type = ?", (surface, entity_type)
                    ).fetchone()[0]
                    stats["entities_matched"] += 1
                row = conn.execute(
                    "SELECT id, name, entity_type, aliases FROM entity"
                    " WHERE id = ?", (entity_id,)).fetchone()
                entity_index[norm] = row
            else:
                entity_id = row["id"]
                stats["entities_matched"] += 1
                # novel surface form (exact string not yet recorded) ->
                # append to aliases; matching stays normalized, growth is
                # verbatim so the table keeps real-world spellings
                aliases = json.loads(row["aliases"] or "[]")
                if surface != row["name"] and surface not in aliases:
                    aliases.append(surface)
                    conn.execute(
                        "UPDATE entity SET aliases = ?, updated_at = ?"
                        " WHERE id = ?",
                        (json.dumps(aliases), now, entity_id))
                    row = conn.execute(
                        "SELECT id, name, entity_type, aliases FROM entity"
                        " WHERE id = ?", (entity_id,)).fetchone()
                    entity_index[norm] = row

            span_start, span_end = _find_span(surface, content_text)
            conn.execute(
                "INSERT INTO entity_mention (document_id, entity_id,"
                " surface, span_start, span_end, method, grade,"
                " extractor_model, prompt_version, created_at)"
                " VALUES (?,?,?,?,?, 'llm', 1, ?, ?, ?)",
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
            cur = conn.execute(
                "INSERT INTO claim (text, first_document_id,"
                " check_worthiness, created_at) VALUES (?,?,?,?)",
                (claim.text, document_id, claim.check_worthiness, now))
            claim_id = int(cur.lastrowid)  # type: ignore[arg-type]
            q_start, q_end = _find_span(claim.quoted_span, content_text)
            conn.execute(
                "INSERT INTO claim_sighting (claim_id, document_id, quote,"
                " quote_start, quote_end, stance, grade, extractor_model,"
                " prompt_version, created_at)"
                " VALUES (?,?,?,?,?, 'asserts', 1, ?, ?, ?)",
                (claim_id, document_id, claim.quoted_span, q_start, q_end,
                 model, prompt_version, now))
            stats["claims"] += 1

        # --- flip the document -------------------------------------------------
        conn.execute(
            "UPDATE document SET enrichment_tier = 1,"
            " enrichment_status = 'done' WHERE id = ?", (document_id,))

    return stats


def mark_failed(conn: sqlite3.Connection, document_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE document SET enrichment_status = 'failed' WHERE id = ?",
            (document_id,))


def mark_queued(conn: sqlite3.Connection, document_ids: list[int]) -> None:
    with conn:
        conn.executemany(
            "UPDATE document SET enrichment_status = 'queued' WHERE id = ?",
            [(d,) for d in document_ids])


def mark_pending(conn: sqlite3.Connection, document_ids: list[int]) -> None:
    with conn:
        conn.executemany(
            "UPDATE document SET enrichment_status = 'pending' WHERE id = ?",
            [(d,) for d in document_ids])
