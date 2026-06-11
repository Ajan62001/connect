"""VectorIndex seam — pgvector add/search round-trip + the disabled
fallback; the factory must never raise. Synthetic vectors only (no model,
no network)."""

from __future__ import annotations

import random

from connect.knowledge.vector import (
    DisabledIndex,
    PgVectorIndex,
    create_vector_index,
)
from connect.storage.pg import utc_now

DIM = 384


async def _docs(pg_conn, n=3) -> list[int]:
    ids = []
    for i in range(1, n + 1):
        cur = await pg_conn.execute(
            "INSERT INTO document (fetched_at, media_type, content_text,"
            " content_hash) VALUES (%s, 'text', 'x', %s) RETURNING id",
            (utc_now(), f"hv{i}"))
        ids.append((await cur.fetchone())["id"])
    return ids


def _vec(seed: int, dim: int = DIM) -> list[float]:
    rng = random.Random(seed)
    return [rng.random() for _ in range(dim)]


async def test_pgvector_cosine_ranking(pg_conn):
    d1, d2, d3 = await _docs(pg_conn)
    index = PgVectorIndex()
    a, b, c = _vec(1), _vec(2), _vec(3)
    await index.add(pg_conn, d1, a, "test")
    await index.add(pg_conn, d2, b, "test")
    await index.add(pg_conn, d3, c, "test")

    results = await index.search(pg_conn, a, k=3)
    assert results[0][0] == d1
    assert abs(results[0][1] - 1.0) < 1e-6  # self-similarity
    assert all(results[i][1] >= results[i + 1][1]
               for i in range(len(results) - 1))


async def test_add_is_upsert(pg_conn):
    (d1,) = await _docs(pg_conn, n=1)
    index = PgVectorIndex()
    await index.add(pg_conn, d1, _vec(1), "test")
    await index.add(pg_conn, d1, _vec(2), "test-2")  # replace, not error
    cur = await pg_conn.execute(
        "SELECT model, count(*) OVER () AS n FROM document_embedding")
    row = await cur.fetchone()
    assert row["n"] == 1 and row["model"] == "test-2"


async def test_disabled_when_embeddings_off(pg_conn):
    index = create_vector_index(enabled=False)
    assert index.backend == "disabled"
    assert isinstance(index, DisabledIndex)
    assert await index.search(pg_conn, _vec(1), k=5) == []
    await index.add(pg_conn, 1, _vec(1), "test")  # no-op, no FK error


def test_factory_picks_pgvector_when_enabled():
    index = create_vector_index(enabled=True)
    assert index.backend == "pgvector"
