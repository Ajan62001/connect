"""Analysis (dossier) read DAO — assembles the /api/analyses contracts
from dossier + dossier_section + evidence rows.

Section-row timestamp convention (pipeline.py): created_at = stage start,
updated_at = stage finish. Claims live inside the verify section's content
JSON (analysis-time snapshot: text, verdict, reasoning, evidence refs with
quotes); the evidence table join only adds display fields (source, tier,
url, title) so the snapshot stays audit-stable even if a later analysis
supersedes an evidence row.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.domain.models import (
    AnalysisClaimItem,
    AnalysisDetail,
    AnalysisEvidenceItem,
    AnalysisListItem,
    AnalysisPage,
    AnalysisStageInfo,
    AnalysisVerdictSummary,
)

_STAGE_ORDER = {"normalize": 0, "verify": 1, "assemble": 2}


def _content(row: Mapping[str, Any]) -> dict[str, Any]:
    parsed = row["content"]
    return parsed if isinstance(parsed, dict) else {}


# the dossier tenancy read predicate (design §5): mine OR shared
_DOSSIER_VISIBLE = "(d.owner_id = %s OR d.visibility = 'shared')"


async def list_page(conn: psycopg.AsyncConnection, *, page: int = 1,
                    page_size: int = 20,
                    viewer: int | None = None) -> AnalysisPage:
    where, params = "", []
    if viewer is not None:
        where = f" AND {_DOSSIER_VISIBLE}"
        params.append(viewer)
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM dossier d"
        " WHERE d.kind = 'analysis'" + where, params)
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT d.id, d.status, d.input_text, d.created_at, d.finished_at,"
        " d.visibility, d.owner_id, u.name AS owner_name"
        " FROM dossier d LEFT JOIN app_user u ON u.id = d.owner_id"
        " WHERE d.kind = 'analysis'" + where +
        " ORDER BY d.id DESC LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    items = [AnalysisListItem(
        id=r["id"], status=r["status"], input_text=r["input_text"],
        created_at=r["created_at"], finished_at=r["finished_at"],
        verdict_summary=await _verdict_summary(conn, r["id"]),
        visibility=r["visibility"], owner_id=r["owner_id"],
        owner_name=r["owner_name"],
    ) for r in rows]
    return AnalysisPage(items=items, total=int(total), page=page,
                        page_size=page_size)


async def _verdict_summary(conn: psycopg.AsyncConnection,
                           dossier_id: int) -> AnalysisVerdictSummary | None:
    cur = await conn.execute(
        "SELECT content FROM dossier_section WHERE dossier_id = %s"
        " AND stage = 'assemble' AND status = 'completed'",
        (dossier_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    summary = _content(row).get("verdict_summary")
    if not isinstance(summary, dict):
        return None
    return AnalysisVerdictSummary(
        **{k: int(summary.get(k, 0))
           for k in ("supported", "refuted", "mixed", "unverified")})


async def get_detail(conn: psycopg.AsyncConnection,
                     dossier_id: int, *,
                     viewer: int | None = None) -> AnalysisDetail | None:
    where, params = "", [dossier_id]
    if viewer is not None:
        where = f" AND {_DOSSIER_VISIBLE}"
        params.append(viewer)
    cur = await conn.execute(
        "SELECT d.id, d.status, d.input_text, d.error, d.created_at,"
        " d.started_at, d.finished_at, d.visibility, d.owner_id,"
        " u.name AS owner_name"
        " FROM dossier d LEFT JOIN app_user u ON u.id = d.owner_id"
        " WHERE d.id = %s AND d.kind = 'analysis'" + where,
        params)
    dossier = await cur.fetchone()
    if dossier is None:
        return None
    cur = await conn.execute(
        "SELECT stage, status, content, created_at, updated_at"
        " FROM dossier_section WHERE dossier_id = %s",
        (dossier_id,))
    sections = sorted(await cur.fetchall(),
                      key=lambda r: _STAGE_ORDER.get(r["stage"], 99))
    stages = []
    claims: list[AnalysisClaimItem] = []
    for section in sections:
        content = _content(section)
        finished = section["updated_at"] \
            if section["status"] in ("completed", "failed") else None
        stages.append(AnalysisStageInfo(
            stage=section["stage"], status=section["status"],
            summary=content.get("summary"),
            started_at=section["created_at"], finished_at=finished))
        if section["stage"] == "verify":
            claims = await _claims(conn, content, viewer=viewer)
    return AnalysisDetail(
        id=dossier["id"], status=dossier["status"],
        input_text=dossier["input_text"], created_at=dossier["created_at"],
        started_at=dossier["started_at"], finished_at=dossier["finished_at"],
        error=dossier["error"], stages=stages, claims=claims,
        last_seq=await last_seq(conn, dossier_id),
        visibility=dossier["visibility"], owner_id=dossier["owner_id"],
        owner_name=dossier["owner_name"])


async def last_seq(conn: psycopg.AsyncConnection, dossier_id: int) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(MAX(ev.seq), 0) AS n FROM job_event ev"
        " JOIN job j ON j.id = ev.job_id WHERE j.dossier_id = %s",
        (dossier_id,))
    return int((await cur.fetchone())["n"])


async def _claims(conn: psycopg.AsyncConnection,
                  verify_content: dict[str, Any], *,
                  viewer: int | None = None) -> list[AnalysisClaimItem]:
    items: list[AnalysisClaimItem] = []
    for claim in verify_content.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        evidence = [await _evidence_item(conn, ref, viewer=viewer)
                    for ref in claim.get("evidence") or []
                    if isinstance(ref, dict)]
        items.append(AnalysisClaimItem(
            id=claim["claim_id"], text=claim["text"], kind=claim["kind"],
            checkable=bool(claim["checkable"]), verdict=claim.get("verdict"),
            confidence=claim.get("confidence"),
            reasoning=claim.get("reasoning"), evidence=evidence))
    return items


async def _evidence_item(conn: psycopg.AsyncConnection,
                         ref: dict[str, Any], *,
                         viewer: int | None = None) -> AnalysisEvidenceItem:
    vis_clause = (" AND (d.visibility = 'shared' OR d.owner_id = %s)"
                  if viewer is not None else "")
    params: tuple = ((ref["document_id"], viewer) if viewer is not None
                     else (ref["document_id"],))
    cur = await conn.execute(
        "SELECT d.url, d.title, s.name AS source_name, s.credibility_tier"
        " FROM document d LEFT JOIN source s ON s.id = d.source_id"
        " WHERE d.id = %s" + vis_clause, params)
    row = await cur.fetchone()
    return AnalysisEvidenceItem(
        id=ref["evidence_id"], document_id=ref["document_id"],
        source_name=row["source_name"] if row else None,
        credibility_tier=row["credibility_tier"] if row else None,
        stance=ref["stance"], confidence=ref.get("relevance"),
        quote=ref.get("quote"),
        url=row["url"] if row else None,
        title=row["title"] if row else None)
