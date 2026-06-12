"""Router-side tenancy helpers (design §5).

Dossier access policy shared by the analyses and investigations routers:
- the read predicate ``owner OR shared`` answers 404 (a private dossier's
  existence is never disclosed);
- mutations (cancel, visibility PATCH) additionally require owner-or-admin
  (403). Private-is-absolute means an admin never even SEES another user's
  private dossier — the 404 fires first.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg
from fastapi import HTTPException

from connect.domain.models import CurrentUser, DossierVisibilityResult, \
    DossierVisibilityUpdate
from connect.knowledge import sharing


async def resolve_dossier(db: psycopg.AsyncConnection, dossier_id: int,
                          kind: str, label: str,
                          user: CurrentUser) -> Mapping[str, Any]:
    """The dossier row IF the user may see it; 404 otherwise."""
    cur = await db.execute(
        "SELECT id, owner_id, visibility, status FROM dossier"
        " WHERE id = %s AND kind = %s"
        " AND (owner_id = %s OR visibility = 'shared')",
        (dossier_id, kind, user.id))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return row


def require_owner_or_admin(row: Mapping[str, Any],
                           user: CurrentUser) -> None:
    if row["owner_id"] != user.id and user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="only the owner (or an admin) may do this")


async def patch_visibility(db: psycopg.AsyncConnection, dossier_id: int,
                           kind: str, label: str, user: CurrentUser,
                           body: DossierVisibilityUpdate,
                           ) -> DossierVisibilityResult:
    """The shared PATCH-visibility flow: resolve (404) -> authorize (403)
    -> share cascade (409s mapped from VisibilityChangeRejected)."""
    row = await resolve_dossier(db, dossier_id, kind, label, user)
    require_owner_or_admin(row, user)
    try:
        return await sharing.change_visibility(
            db, dossier_id=dossier_id, owner_id=row["owner_id"],
            current=row["visibility"], target=body.visibility,
            confirm_documents=body.confirm_documents)
    except sharing.VisibilityChangeRejected as e:
        raise HTTPException(status_code=409, detail={
            "reason": e.reason,
            "message": e.detail,
            "documents": [d.model_dump() for d in e.documents],
        }) from e
