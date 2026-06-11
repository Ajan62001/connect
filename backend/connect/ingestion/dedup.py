"""Dedup: sha256 exact + 64-bit simhash near-dup against a recent window.

Reuses the pure functions from knowledge/enrichment/t0.py (the T0 owner).
Near-dups (Hamming <= 3 within the last N days) get a canonical_document_id
pointer and enrichment_status='skipped_dup' — the syndication killer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from connect.knowledge.enrichment.t0 import (
    content_hash,
    from_signed64,
    hamming,
    normalize_text,
    simhash64,
    to_signed64,
)
from connect.storage import documents as doc_dao

__all__ = [
    "content_hash", "normalize_text", "simhash64", "hamming",
    "to_signed64", "from_signed64", "find_near_duplicate",
]


def find_near_duplicate(conn: sqlite3.Connection, fingerprint: int, *,
                        window_days: int = 14,
                        max_hamming: int = 3) -> int | None:
    """Return the canonical document id of a near-duplicate within the
    window, or None. ``fingerprint`` is the unsigned 64-bit simhash."""
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)
             ).strftime("%Y-%m-%dT%H:%M:%S")
    for doc_id, stored, canonical_id in doc_dao.recent_simhashes(conn, since):
        if hamming(from_signed64(stored), fingerprint) <= max_hamming:
            return canonical_id or doc_id
    return None
