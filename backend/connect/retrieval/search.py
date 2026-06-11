"""Corpus retrieval — the hybrid layer (design v02-postgres-port §4).

Documents: tsvector ranking (ts_rank_cd over the GIN index) RRF-fused with
pgvector KNN over document_embedding, fused in Python with k=60 — NOT one
mega-SQL — because the VectorIndex seam must stay a seam: the
DisabledIndex/NullEmbedder path degrades hybrid -> lexical-only with zero
branching beyond an empty list. The fused page is hydrated in one
``id = ANY(%s)`` query with escaped ts_headline snippets (vector-only hits
get the leading fragment). Entities: ILIKE+trigram substring parity.

Kept as its own layer so the API and the analysis/investigation
evidence-gathering stages never talk the FTS dialect directly — they call
``hybrid_document_ids`` / ``rrf_fuse`` here.
"""

from __future__ import annotations

import logging
from typing import Literal, Sequence

import psycopg

from connect.domain.models import SearchResult
from connect.knowledge.embedder import Embedder
from connect.knowledge.vector import VectorIndex
from connect.storage import entities as entity_dao
from connect.storage import fts as fts_dao

log = logging.getLogger(__name__)

SearchKind = Literal["all", "documents", "entities"]

RRF_K = 60          # the standard reciprocal-rank-fusion constant
POOL_K = 50         # candidates fed to fusion per leg (lexical / vector)


def rrf_fuse(rank_lists: Sequence[Sequence[int]], k: int = RRF_K,
             ) -> list[int]:
    """Reciprocal-rank fusion over id rank lists; best-first, ties by id."""
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for rank, item in enumerate(ranks):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: (-scores[i], i))


async def hybrid_document_ids(conn: psycopg.AsyncConnection, q: str, *,
                              embedder: Embedder | None,
                              vectors: VectorIndex | None,
                              lexical_k: int = POOL_K,
                              vector_k: int = POOL_K) -> list[int]:
    """Fused document ids, best-first: lexical ranking + vector KNN under
    RRF. Either leg degrades to an empty list (no embedder/vectors, a
    no-vector embedder, or a lexical error) — fusion of one list is that
    list, so lexical-only deployments keep exact v0.1 ordering."""
    lex_ids: list[int] = []
    try:
        lex_ids = await fts_dao.rank_documents(conn, q, limit=lexical_k)
    except psycopg.Error:
        log.exception("lexical ranking failed")
    vec_ids: list[int] = []
    if vectors is not None and embedder is not None:
        vecs = embedder.embed([q])
        if vecs:
            vec_ids = [doc_id for doc_id, _sim in
                       await vectors.search(conn, vecs[0], k=vector_k)]
    return rrf_fuse([lex_ids, vec_ids]) if vec_ids else lex_ids


async def search(conn: psycopg.AsyncConnection, q: str, *,
                 kind: SearchKind = "all",
                 limit: int = 20,
                 embedder: Embedder | None = None,
                 vectors: VectorIndex | None = None) -> SearchResult:
    documents, total_documents = [], 0
    entities, total_entities = [], 0
    if kind in ("all", "documents"):
        fused = await hybrid_document_ids(
            conn, q, embedder=embedder, vectors=vectors,
            lexical_k=max(POOL_K, limit), vector_k=max(POOL_K, limit))
        documents = await fts_dao.hydrate_page(conn, q, fused[:limit])
        # the corpus-wide lexical count, floored by what fusion actually
        # surfaced (vector-only hits are results the count can't see)
        total_documents = max(await fts_dao.count_documents(conn, q),
                              len(fused))
    if kind in ("all", "entities"):
        entities, total_entities = await entity_dao.search(conn, q,
                                                           limit=limit)
    return SearchResult(
        documents=documents, entities=entities,
        total_documents=total_documents, total_entities=total_entities,
        total=total_documents)
