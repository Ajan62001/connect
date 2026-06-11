"""VectorIndex seam — sqlite-vec when available, brute-force fallback.

This machine's SQLite (3.37) loads extensions but sqlite-vec generally needs
>= 3.41, so the factory TRIES sqlite-vec and falls back to a plain
``document_embedding`` BLOB table with pure-Python cosine (fine at
single-user corpus scale). Vector availability never breaks startup.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import struct
from typing import Protocol

from connect.storage.schema import VEC_DOCUMENT_DDL

log = logging.getLogger(__name__)


class VectorIndex(Protocol):
    backend: str  # "sqlite-vec" | "bruteforce" | "disabled"

    def add(self, doc_id: int, vector: list[float], model: str) -> None: ...

    def search(self, vector: list[float], k: int = 10) -> list[tuple[int, float]]:
        """[(document_id, similarity)] best-first."""
        ...


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class DisabledIndex:
    backend = "disabled"

    def add(self, doc_id: int, vector: list[float], model: str) -> None:
        pass

    def search(self, vector: list[float], k: int = 10) -> list[tuple[int, float]]:
        return []


class SqliteVecIndex:
    """sqlite-vec vec0 virtual table; raises at construction if unavailable."""

    backend = "sqlite-vec"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        import sqlite_vec  # may be uninstalled — caller catches

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        conn.execute(VEC_DOCUMENT_DDL)

    def add(self, doc_id: int, vector: list[float], model: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO vec_document(rowid, embedding)"
                " VALUES (?, ?)", (doc_id, _pack(vector)))

    def search(self, vector: list[float], k: int = 10) -> list[tuple[int, float]]:
        rows = self._conn.execute(
            "SELECT rowid, distance FROM vec_document"
            " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (_pack(vector), k)).fetchall()
        # vec0 returns a distance; expose similarity-ish = -distance
        return [(int(r[0]), -float(r[1])) for r in rows]


class BruteForceIndex:
    """Plain BLOB table + cosine in pure Python (no numpy)."""

    backend = "bruteforce"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn  # document_embedding table is part of schema v1

    def add(self, doc_id: int, vector: list[float], model: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO document_embedding"
                " (document_id, model, dim, vector) VALUES (?,?,?,?)",
                (doc_id, model, len(vector), _pack(vector)))

    def search(self, vector: list[float], k: int = 10) -> list[tuple[int, float]]:
        q_norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        scored: list[tuple[int, float]] = []
        for row in self._conn.execute(
                "SELECT document_id, vector FROM document_embedding"):
            v = _unpack(row[1])
            if len(v) != len(vector):
                continue
            dot = sum(a * b for a, b in zip(vector, v))
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            scored.append((int(row[0]), dot / (q_norm * norm)))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]


def create_vector_index(conn: sqlite3.Connection, *, enabled: bool) -> VectorIndex:
    """Never raises: sqlite-vec -> bruteforce -> disabled."""
    if not enabled:
        return DisabledIndex()
    try:
        index = SqliteVecIndex(conn)
        log.info("vector backend: sqlite-vec")
        return index
    except Exception as e:  # noqa: BLE001 — expected on SQLite < 3.41
        log.info("sqlite-vec unavailable (%s); using brute-force fallback", e)
        return BruteForceIndex(conn)
