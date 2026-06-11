from __future__ import annotations

from fastapi import APIRouter, Depends

from connect.api.deps import get_container
from connect.domain.models import Health
from connect.orchestration.container import Container

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health)
async def health(container: Container = Depends(get_container)) -> Health:
    return Health(
        ok=True,
        schema_version=container.schema_version,
        db_path=str(container.settings.db_path),
        vector_backend=container.vector_backend,  # type: ignore[arg-type]
    )
