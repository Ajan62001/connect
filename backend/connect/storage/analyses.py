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


async def list_page(conn: psycopg.AsyncConnection, *, page: int = 1,
                    page_size: int = 20) -> AnalysisPage:
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM dossier WHERE kind = 'analysis'")
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT id, status, input_text, created_at, finished_at"
        " FROM dossier WHERE kind = 'analysis'"
        " ORDER BY id DESC LIMIT %s OFFSET %s",
        (page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    items = [AnalysisListItem(
        id=r["id"], status=r["status"], input_text=r["input_text"],
        created_at=r["created_at"], finished_at=r["finished_at"],
        verdict_summary=await _verdict_summary(conn, r["id"]),
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
                     dossier_id: int) -> AnalysisDetail | None:
    cur = await conn.execute(
        "SELECT id, status, input_text, error, created_at, started_at,"
        " finished_at FROM dossier WHERE id = %s AND kind = 'analysis'",
        (dossier_id,))
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
            claims = await _claims(conn, content)
    return AnalysisDetail(
        id=dossier["id"], status=dossier["status"],
        input_text=dossier["input_text"], created_at=dossier["created_at"],
        started_at=dossier["started_at"], finished_at=dossier["finished_at"],
        error=dossier["error"], stages=stages, claims=claims,
        last_seq=await last_seq(conn, dossier_id))


async def last_seq(conn: psycopg.AsyncConnection, dossier_id: int) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(MAX(ev.seq), 0) AS n FROM job_event ev"
        " JOIN job j ON j.id = ev.job_id WHERE j.dossier_id = %s",
        (dossier_id,))
    return int((await cur.fetchone())["n"])


async def _claims(conn: psycopg.AsyncConnection,
                  verify_content: dict[str, Any]) -> list[AnalysisClaimItem]:
    items: list[AnalysisClaimItem] = []
    for claim in verify_content.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        evidence = [await _evidence_item(conn, ref)
                    for ref in claim.get("evidence") or []
                    if isinstance(ref, dict)]
        items.append(AnalysisClaimItem(
            id=claim["claim_id"], text=claim["text"], kind=claim["kind"],
            checkable=bool(claim["checkable"]), verdict=claim.get("verdict"),
            confidence=claim.get("confidence"),
            reasoning=claim.get("reasoning"), evidence=evidence))
    return items


async def _evidence_item(conn: psycopg.AsyncConnection,
                         ref: dict[str, Any]) -> AnalysisEvidenceItem:
    cur = await conn.execute(
        "SELECT d.url, d.title, s.name AS source_name, s.credibility_tier"
        " FROM document d LEFT JOIN source s ON s.id = d.source_id"
        " WHERE d.id = %s", (ref["document_id"],))
    row = await cur.fetchone()
    return AnalysisEvidenceItem(
        id=ref["evidence_id"], document_id=ref["document_id"],
        source_name=row["source_name"] if row else None,
        credibility_tier=row["credibility_tier"] if row else None,
        stance=ref["stance"], confidence=ref.get("relevance"),
        quote=ref.get("quote"),
        url=row["url"] if row else None,
        title=row["title"] if row else None)
