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

import json
import sqlite3
from typing import Any

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


def _content(row: sqlite3.Row) -> dict[str, Any]:
    try:
        parsed = json.loads(row["content"] or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def list_page(conn: sqlite3.Connection, *, page: int = 1,
              page_size: int = 20) -> AnalysisPage:
    total = conn.execute("SELECT COUNT(*) FROM dossier"
                         " WHERE kind = 'analysis'").fetchone()[0]
    rows = conn.execute(
        "SELECT id, status, input_text, created_at, finished_at"
        " FROM dossier WHERE kind = 'analysis'"
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size)).fetchall()
    items = [AnalysisListItem(
        id=r["id"], status=r["status"], input_text=r["input_text"],
        created_at=r["created_at"], finished_at=r["finished_at"],
        verdict_summary=_verdict_summary(conn, r["id"]),
    ) for r in rows]
    return AnalysisPage(items=items, total=int(total), page=page,
                        page_size=page_size)


def _verdict_summary(conn: sqlite3.Connection,
                     dossier_id: int) -> AnalysisVerdictSummary | None:
    row = conn.execute(
        "SELECT content FROM dossier_section WHERE dossier_id = ?"
        " AND stage = 'assemble' AND status = 'completed'",
        (dossier_id,)).fetchone()
    if row is None:
        return None
    summary = _content(row).get("verdict_summary")
    if not isinstance(summary, dict):
        return None
    return AnalysisVerdictSummary(
        **{k: int(summary.get(k, 0))
           for k in ("supported", "refuted", "mixed", "unverified")})


def get_detail(conn: sqlite3.Connection,
               dossier_id: int) -> AnalysisDetail | None:
    dossier = conn.execute(
        "SELECT id, status, input_text, error, created_at, started_at,"
        " finished_at FROM dossier WHERE id = ? AND kind = 'analysis'",
        (dossier_id,)).fetchone()
    if dossier is None:
        return None
    sections = conn.execute(
        "SELECT stage, status, content, created_at, updated_at"
        " FROM dossier_section WHERE dossier_id = ?",
        (dossier_id,)).fetchall()
    sections = sorted(sections,
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
            claims = _claims(conn, content)
    return AnalysisDetail(
        id=dossier["id"], status=dossier["status"],
        input_text=dossier["input_text"], created_at=dossier["created_at"],
        started_at=dossier["started_at"], finished_at=dossier["finished_at"],
        error=dossier["error"], stages=stages, claims=claims,
        last_seq=last_seq(conn, dossier_id))


def last_seq(conn: sqlite3.Connection, dossier_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(ev.seq), 0) FROM job_event ev"
        " JOIN job j ON j.id = ev.job_id WHERE j.dossier_id = ?",
        (dossier_id,)).fetchone()
    return int(row[0])


def _claims(conn: sqlite3.Connection,
            verify_content: dict[str, Any]) -> list[AnalysisClaimItem]:
    items: list[AnalysisClaimItem] = []
    for claim in verify_content.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        evidence = [_evidence_item(conn, ref)
                    for ref in claim.get("evidence") or []
                    if isinstance(ref, dict)]
        items.append(AnalysisClaimItem(
            id=claim["claim_id"], text=claim["text"], kind=claim["kind"],
            checkable=bool(claim["checkable"]), verdict=claim.get("verdict"),
            confidence=claim.get("confidence"),
            reasoning=claim.get("reasoning"), evidence=evidence))
    return items


def _evidence_item(conn: sqlite3.Connection,
                   ref: dict[str, Any]) -> AnalysisEvidenceItem:
    row = conn.execute(
        "SELECT d.url, d.title, s.name AS source_name, s.credibility_tier"
        " FROM document d LEFT JOIN source s ON s.id = d.source_id"
        " WHERE d.id = ?", (ref["document_id"],)).fetchone()
    return AnalysisEvidenceItem(
        id=ref["evidence_id"], document_id=ref["document_id"],
        source_name=row["source_name"] if row else None,
        credibility_tier=row["credibility_tier"] if row else None,
        stance=ref["stance"], confidence=ref.get("relevance"),
        quote=ref.get("quote"),
        url=row["url"] if row else None,
        title=row["title"] if row else None)
