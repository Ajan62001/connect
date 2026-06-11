"""Row factories shared by the Phase 2 tests (clustering, threading,
promotion, briefing). Synthetic embeddings are injected directly into
document_embedding — no model downloads ever.

PG vectors are a fixed 384 dims (vector(384)); the synthetic test vectors
are padded with zeros to fit (cosine over the padded vectors is identical
to cosine over the originals).
"""

from __future__ import annotations

from itertools import count

import psycopg

from connect.storage.pg import Jsonb, Vector, utc_now

_seq = count(1)

VECTOR_DIM = 384


def pad(vec: list[float]) -> list[float]:
    """Zero-pad a synthetic test vector to the schema's 384 dims."""
    assert len(vec) <= VECTOR_DIM
    return list(vec) + [0.0] * (VECTOR_DIM - len(vec))


async def insert_source(conn: psycopg.AsyncConnection, name: str, *,
                        tier: int = 2, notes: str | None = None,
                        config: dict | None = None) -> int:
    cur = await conn.execute(
        "INSERT INTO source (name, type, config, credibility_tier,"
        " notes, created_at) VALUES (%s, 'manual', %s, %s, %s, %s)"
        " RETURNING id",
        (name, Jsonb(config or {}), tier, notes, utc_now()))
    return int((await cur.fetchone())["id"])


async def insert_doc(conn: psycopg.AsyncConnection, *, title: str = "Doc",
                     text: str = "body text", source_id: int | None = None,
                     published_at: str | None = None,
                     watch_hit: bool = False) -> int:
    cur = await conn.execute(
        "INSERT INTO document (source_id, title, published_at,"
        " fetched_at, media_type, content_text, content_hash,"
        " enrichment_status, watch_hit)"
        " VALUES (%s,%s,%s,%s, 'text', %s, %s, 'pending', %s) RETURNING id",
        (source_id, title, published_at, utc_now(), text,
         f"h-{next(_seq)}", bool(watch_hit)))
    return int((await cur.fetchone())["id"])


async def add_t1(conn: psycopg.AsyncConnection, doc_id: int, *,
                 summary: str = "One-line summary.",
                 event_type: str = "other",
                 model: str = "claude-haiku-4-5") -> None:
    await conn.execute(
        "INSERT INTO document_enrichment (document_id,"
        " summary, event_type, model, prompt_version, created_at)"
        " VALUES (%s,%s,%s,%s, 't1-v1', %s)"
        " ON CONFLICT (document_id) DO UPDATE SET"
        " summary=EXCLUDED.summary, event_type=EXCLUDED.event_type,"
        " model=EXCLUDED.model, prompt_version=EXCLUDED.prompt_version,"
        " created_at=EXCLUDED.created_at",
        (doc_id, summary, event_type, model, utc_now()))
    await conn.execute(
        "UPDATE document SET enrichment_status='done',"
        " enrichment_tier = GREATEST(enrichment_tier, 1) WHERE id = %s",
        (doc_id,))


async def entity_id(conn: psycopg.AsyncConnection, name: str,
                    etype: str = "organization") -> int:
    cur = await conn.execute(
        "SELECT id FROM entity WHERE name = %s AND entity_type = %s",
        (name, etype))
    row = await cur.fetchone()
    if row is not None:
        return int(row["id"])
    cur = await conn.execute(
        "INSERT INTO entity (name, entity_type, aliases, created_at)"
        " VALUES (%s, %s, '[]', %s) RETURNING id",
        (name, etype, utc_now()))
    return int((await cur.fetchone())["id"])


async def add_mentions(conn: psycopg.AsyncConnection, doc_id: int,
                       names: tuple[str, ...]) -> list[int]:
    ids = []
    for name in names:
        eid = await entity_id(conn, name)
        await conn.execute(
            "INSERT INTO entity_mention (document_id, entity_id,"
            " surface, method, created_at) VALUES (%s,%s,%s, 'llm', %s)",
            (doc_id, eid, name, utc_now()))
        ids.append(eid)
    return ids


async def set_vector(conn: psycopg.AsyncConnection, doc_id: int,
                     vec: list[float]) -> None:
    await conn.execute(
        "INSERT INTO document_embedding (document_id, model, embedding)"
        " VALUES (%s, 'test', %s)"
        " ON CONFLICT (document_id) DO UPDATE SET"
        " model=EXCLUDED.model, embedding=EXCLUDED.embedding",
        (doc_id, Vector(pad(vec))))


async def t1_doc(conn: psycopg.AsyncConnection, *, title: str = "Doc",
                 summary: str = "One-line summary.",
                 event_type: str = "other",
                 entities: tuple[str, ...] = (),
                 vec: list[float] | None = None,
                 source_id: int | None = None,
                 published_at: str | None = None,
                 watch_hit: bool = False) -> int:
    """One T1-enriched document with mentions and an injected embedding —
    the exact input shape T2 expects."""
    doc_id = await insert_doc(conn, title=title, source_id=source_id,
                              published_at=published_at,
                              watch_hit=watch_hit)
    await add_t1(conn, doc_id, summary=summary, event_type=event_type)
    if entities:
        await add_mentions(conn, doc_id, entities)
    if vec is not None:
        await set_vector(conn, doc_id, vec)
    return doc_id
