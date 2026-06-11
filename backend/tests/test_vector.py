"""VectorIndex seam — brute-force fallback with synthetic vectors (no model,
no network); the factory must never raise."""

from __future__ import annotations

import random

import pytest

from connect.knowledge.vector import BruteForceIndex, create_vector_index
from connect.storage import db as db_mod


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "v.db")
    db_mod.init_db(c)
    for i in (1, 2, 3):
        c.execute(
            "INSERT INTO document (id, fetched_at, media_type, content_text,"
            " content_hash) VALUES (?, '2026-01-01T00:00:00Z', 'text', 'x', ?)",
            (i, f"h{i}"))
    c.commit()
    yield c
    c.close()


def _vec(seed: int, dim: int = 16) -> list[float]:
    rng = random.Random(seed)
    return [rng.random() for _ in range(dim)]


def test_bruteforce_cosine_ranking(conn):
    index = BruteForceIndex(conn)
    a, b, c = _vec(1), _vec(2), _vec(3)
    index.add(1, a, "test")
    index.add(2, b, "test")
    index.add(3, c, "test")

    results = index.search(a, k=3)
    assert results[0][0] == 1
    assert abs(results[0][1] - 1.0) < 1e-6  # self-similarity
    assert all(results[i][1] >= results[i + 1][1]
               for i in range(len(results) - 1))


def test_disabled_when_embeddings_off(conn):
    index = create_vector_index(conn, enabled=False)
    assert index.backend == "disabled"
    assert index.search(_vec(1), k=5) == []


def test_factory_never_raises(conn):
    index = create_vector_index(conn, enabled=True)
    assert index.backend in ("sqlite-vec", "bruteforce")
