"""Embedder seam — local embeddings behind a Protocol.

FastEmbedEmbedder lazily loads BAAI/bge-small-en-v1.5 (384-dim, ONNX, CPU)
on FIRST USE — never at import or construction, so tests and offline boots
never download a model. Any load failure degrades to producing no vectors;
embedding availability must never break ingestion.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text; empty list means 'unavailable'."""
        ...


class NullEmbedder:
    """Embeddings disabled (config) or permanently failed."""

    model_name = "null"
    dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        return []


class FastEmbedEmbedder:
    """Lazy fastembed wrapper; degrades to no-op if the model can't load."""

    def __init__(self, model_name: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM):
        self.model_name = model_name
        self.dim = dim
        self._model = None
        self._failed = False

    def _ensure_model(self):
        if self._model is None and not self._failed:
            try:
                from fastembed import TextEmbedding  # lazy heavy import
                self._model = TextEmbedding(model_name=self.model_name)
            except Exception:  # noqa: BLE001 — degrade, never break ingest
                log.exception("fastembed model load failed; embeddings disabled")
                self._failed = True
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        if model is None:
            return []
        try:
            return [list(map(float, v)) for v in model.embed(texts)]
        except Exception:  # noqa: BLE001
            log.exception("embedding failed")
            return []
