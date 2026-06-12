"""Dedup: sha256 exact + 64-bit simhash near-dup against a recent window.

Reuses the pure functions from knowledge/enrichment/t0.py (the T0 owner).
Near-dups (Hamming <= 3 within the last N days) get a canonical_document_id
pointer and enrichment_status='skipped_dup' — the syndication killer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

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


async def find_near_duplicate(conn: psycopg.AsyncConnection,
                              fingerprint: int, *,
                              window_days: int = 14,
                              max_hamming: int = 3,
                              owner_id: int | None = None) -> int | None:
    """Return the canonical document id of a near-duplicate within the
    window, or None. ``fingerprint`` is the unsigned 64-bit simhash.

    Tenancy: candidates are shared docs plus the ingesting owner's own
    private docs — a new doc never canonicalizes onto a private doc its
    ingester cannot see."""
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)
             ).strftime("%Y-%m-%dT%H:%M:%S")
    for doc_id, stored, canonical_id in await doc_dao.recent_simhashes(
            conn, since, owner_id=owner_id):
        if hamming(from_signed64(stored), fingerprint) <= max_hamming:
            return canonical_id or doc_id
    return None
