"""T0 enrichment — free, code-only, pure functions.

Owns text normalization, the content hash, and the inline 64-bit simhash
(token 3-shingles). ingestion/dedup.py reuses these; nothing here touches
the DB or the network.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

SIMHASH_BITS = 64


def normalize_text(text: str) -> str:
    """Unicode NFKC + whitespace collapse. The canonical form everything
    (hashing, simhash, storage of pasted text) derives from."""
    text = unicodedata.normalize("NFKC", text)
    return _WS_RE.sub(" ", text).strip()


def content_hash(text: str) -> str:
    """sha256 over the normalized text — the exact-dedup key."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _shingles(text: str, k: int = 3) -> list[str]:
    tokens = _TOKEN_RE.findall(normalize_text(text).lower())
    if len(tokens) < k:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1)]


def _hash64(s: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def simhash64(text: str) -> int:
    """64-bit simhash over token 3-shingles (unsigned 0..2^64-1).

    Near-duplicate articles (syndicated wire copy with minor edits) land
    within a small Hamming distance; unrelated texts are ~32 bits apart.
    """
    counts = [0] * SIMHASH_BITS
    for shingle in _shingles(text):
        h = _hash64(shingle)
        for bit in range(SIMHASH_BITS):
            counts[bit] += 1 if (h >> bit) & 1 else -1
    fingerprint = 0
    for bit in range(SIMHASH_BITS):
        if counts[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def to_signed64(value: int) -> int:
    """SQLite INTEGER is signed 64-bit; map the unsigned fingerprint into it."""
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed64(value: int) -> int:
    return value + (1 << 64) if value < 0 else value
