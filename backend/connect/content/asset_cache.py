"""Disk cache for fetched scene assets (photos + b-roll clips).

Every successful fetch is written under ``blob_dir/asset_cache`` keyed by a
hash of (kind, query, variant, …) so the NEXT render of the same subject —
in any process: the api-embedded worker, a standalone worker, tomorrow's
factory run — reuses the local copy instead of re-downloading. This is what
makes repeated renders fast, keeps b-roll stable across re-renders of the
same script, and protects the Pexels request quota.

Only positive results are persisted (a miss should be retried next process
— the sources grow). The cache is size-capped: writes prune the oldest
files (mtime, refreshed on every hit) once the directory exceeds
``asset_cache_mb``. Setting ``asset_cache_mb=0`` disables the disk layer
entirely. Every operation fails soft — a broken cache must never break a
render.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_SUFFIX = ".bin"


def _dir(settings) -> Path | None:
    base = getattr(settings, "blob_dir", None)
    if not base or not getattr(settings, "asset_cache_mb", 0):
        return None
    try:
        d = Path(base) / "asset_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def _path(d: Path, kind: str, key: str) -> Path:
    h = hashlib.sha256(f"{kind}|{key}".encode()).hexdigest()
    return d / f"{h}{_SUFFIX}"


def get(settings, kind: str, key: str) -> bytes | None:
    """The cached asset bytes, or None. A hit refreshes mtime (LRU)."""
    d = _dir(settings)
    if d is None:
        return None
    p = _path(d, kind, key)
    try:
        data = p.read_bytes()
        os.utime(p)
        return data
    except OSError:
        return None


def put(settings, kind: str, key: str, data: bytes | None) -> None:
    """Persist a fetched asset; prunes oldest files past the size cap."""
    d = _dir(settings)
    if d is None or not data:
        return
    p = _path(d, kind, key)
    try:
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    except OSError as e:  # noqa: BLE001 — cache is best-effort
        log.info("asset cache write failed: %s", e)
        return
    _prune(d, int(getattr(settings, "asset_cache_mb", 0)) * 1024 * 1024)


def _prune(d: Path, cap_bytes: int) -> None:
    if cap_bytes <= 0:
        return
    try:
        files = []
        total = 0
        for f in d.iterdir():
            if f.suffix != _SUFFIX:
                continue
            st = f.stat()
            files.append((st.st_mtime, st.st_size, f))
            total += st.st_size
    except OSError:
        return
    if total <= cap_bytes:
        return
    for _, size, f in sorted(files):
        try:
            f.unlink()
        except OSError:
            continue
        total -= size
        if total <= cap_bytes:
            break
