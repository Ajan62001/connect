"""Shared fixtures. NO network, NO model downloads: poller off, embeddings
off — enforced through Settings, not monkeypatching."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from connect.api.main import create_app
from connect.orchestration.config import Settings
from connect.orchestration.container import Container


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "connect.db",
        blob_dir=tmp_path / "blobs",
        poller_enabled=False,
        embeddings_enabled=False,
        # never auto-queue LLM work in tests (backend/.env may carry a real
        # ANTHROPIC_API_KEY); tests inject MockProvider explicitly instead
        enrich_fast_path_enabled=False,
    )


@pytest.fixture()
def container(settings) -> Container:
    c = Container(settings)
    c.startup()
    yield c
    c.db.close()


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
