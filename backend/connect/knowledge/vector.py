"""VectorIndex seam — pgvector-backed, with a DisabledIndex for tests.

v0.2: sqlite-vec / the brute-force BLOB fallback die; `vector(384)` columns
+ HNSW live in the same database (document_embedding). Methods take the
caller's connection (the seam carries no connection state), are async, and
similarity is cosine (1 - the `<=>` distance) — best-first, same contract
as v0.1.
"""

from __future__ import annotations

import logging
from typing import Protocol

import psycopg

from connect.storage.pg import Vector

log = logging.getLogger(__name__)


class VectorIndex(Protocol):
    backend: str  # "pgvector" | "disabled"

    async def add(self, conn: psycopg.AsyncConnection, doc_id: int,
                  vector: list[float], model: str) -> None: ...

    async def search(self, conn: psycopg.AsyncConnection,
                     vector: list[float], k: int = 10,
                     ) -> list[tuple[int, float]]:
        """[(document_id, similarity)] best-first."""
        ...


class DisabledIndex:
    backend = "disabled"

    async def add(self, conn: psycopg.AsyncConnection, doc_id: int,
                  vector: list[float], model: str) -> None:
        pass

    async def search(self, conn: psycopg.AsyncConnection,
                     vector: list[float], k: int = 10,
                     ) -> list[tuple[int, float]]:
        return []


class PgVectorIndex:
    """document_embedding upsert + HNSW-served cosine nearest-neighbour."""

    backend = "pgvector"

    async def add(self, conn: psycopg.AsyncConnection, doc_id: int,
                  vector: list[float], model: str) -> None:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO document_embedding (document_id, model,"
                " embedding) VALUES (%s, %s, %s)"
                " ON CONFLICT (document_id) DO UPDATE SET"
                " model = EXCLUDED.model, embedding = EXCLUDED.embedding",
                (doc_id, model, Vector(vector)))

    async def search(self, conn: psycopg.AsyncConnection,
                     vector: list[float], k: int = 10,
                     ) -> list[tuple[int, float]]:
        probe = Vector(vector)
        cur = await conn.execute(
            "SELECT document_id, 1 - (embedding <=> %s) AS sim"
            " FROM document_embedding ORDER BY embedding <=> %s LIMIT %s",
            (probe, probe, k))
        return [(int(r["document_id"]), float(r["sim"]))
                for r in await cur.fetchall()]


def create_vector_index(*, enabled: bool) -> VectorIndex:
    """pgvector when embeddings are on; DisabledIndex otherwise."""
    if not enabled:
        return DisabledIndex()
    return PgVectorIndex()
