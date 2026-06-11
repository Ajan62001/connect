"""Content-addressed blob store: data/blobs/<sha256[:2]>/<sha256>.

Documents are immutable snapshots; the DB stores only the relative path.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class BlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, data: bytes) -> str:
        """Store bytes; returns the path RELATIVE to the blob root
        (e.g. 'ab/abcdef...'). Idempotent by construction."""
        sha = hashlib.sha256(data).hexdigest()
        rel = f"{sha[:2]}/{sha}"
        path = self.root / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return rel

    def get(self, rel_path: str) -> bytes:
        return (self.root / rel_path).read_bytes()

    def exists(self, rel_path: str) -> bool:
        return (self.root / rel_path).exists()
