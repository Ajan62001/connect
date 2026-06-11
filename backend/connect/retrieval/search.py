"""Corpus retrieval — FTS5/BM25 over documents + LIKE over entity
names/aliases; hybrid RRF fusion with the vector index lands when the
KB-RAG phase needs it. Kept as its own layer so the API and (later)
analysis stages never talk FTS directly."""

from __future__ import annotations

import sqlite3
from typing import Literal

from connect.domain.models import SearchResult
from connect.storage import entities as entity_dao
from connect.storage import fts as fts_dao

SearchKind = Literal["all", "documents", "entities"]


def search(conn: sqlite3.Connection, q: str, *, kind: SearchKind = "all",
           limit: int = 20) -> SearchResult:
    documents, total_documents = [], 0
    entities, total_entities = [], 0
    if kind in ("all", "documents"):
        documents, total_documents = fts_dao.search_documents(
            conn, q, page=1, page_size=limit)
    if kind in ("all", "entities"):
        entities, total_entities = entity_dao.search(conn, q, limit=limit)
    return SearchResult(
        documents=documents, entities=entities,
        total_documents=total_documents, total_entities=total_entities,
        total=total_documents)
