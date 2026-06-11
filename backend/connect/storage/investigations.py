"""Investigation (dossier kind='investigation') read DAO — assembles the
/api/investigations contracts from dossier + dossier_section + question +
finding (+ finding_evidence) rows."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from connect.investigation.schema import (
    FindingEvidenceInfo,
    FindingInfo,
    InvestigationCounts,
    InvestigationDetail,
    InvestigationListItem,
    InvestigationPage,
    QuestionInfo,
    StageInfo,
)

_STAGE_ORDER = {"scope": 0, "investigate": 1, "synthesize": 2}
SECTION_STAGES = ("scope", "investigate", "timeline", "causal_narrative",
                  "actors", "alternatives", "open_questions", "watch_next")


def _content(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def counts_for(conn: sqlite3.Connection,
               dossier_id: int) -> InvestigationCounts:
    findings = conn.execute(
        "SELECT COUNT(*) FROM finding WHERE dossier_id = ?",
        (dossier_id,)).fetchone()[0]
    open_q = conn.execute(
        "SELECT COUNT(*) FROM question WHERE dossier_id = ?"
        " AND status IN ('open', 'partial')", (dossier_id,)).fetchone()[0]
    answered = conn.execute(
        "SELECT COUNT(*) FROM question WHERE dossier_id = ?"
        " AND status = 'answered'", (dossier_id,)).fetchone()[0]
    usage = _content(conn.execute(
        "SELECT model_usage FROM dossier WHERE id = ?",
        (dossier_id,)).fetchone()["model_usage"])
    docs_added = usage.get("docs_added")
    return InvestigationCounts(
        findings=int(findings), questions_open=int(open_q),
        questions_answered=int(answered),
        docs_added=int(docs_added) if isinstance(docs_added, int) else 0)


def list_page(conn: sqlite3.Connection, *, page: int = 1,
              page_size: int = 20) -> InvestigationPage:
    total = conn.execute(
        "SELECT COUNT(*) FROM dossier WHERE kind = 'investigation'",
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT id, status, title, input_text, input_type, created_at,"
        " finished_at FROM dossier WHERE kind = 'investigation'"
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size)).fetchall()
    items = [InvestigationListItem(
        id=r["id"], status=r["status"], title=r["title"],
        input_text=r["input_text"], input_type=r["input_type"],
        created_at=r["created_at"], finished_at=r["finished_at"],
        counts=counts_for(conn, r["id"]),
    ) for r in rows]
    return InvestigationPage(items=items, total=int(total), page=page,
                             page_size=page_size)


def questions_for(conn: sqlite3.Connection,
                  dossier_id: int) -> list[QuestionInfo]:
    rows = conn.execute(
        "SELECT * FROM question WHERE dossier_id = ? ORDER BY id",
        (dossier_id,)).fetchall()
    out: list[QuestionInfo] = []
    for r in rows:
        try:
            finding_ids = [int(f) for f in
                           json.loads(r["answer_finding_ids"] or "[]")]
        except (ValueError, TypeError):
            finding_ids = []
        out.append(QuestionInfo(
            id=r["id"], qtype=r["qtype"], text=r["text"],
            about_type=r["about_type"], about_id=r["about_id"],
            status=r["status"], priority=r["priority"],
            answer_summary=r["answer_summary"],
            answer_finding_ids=finding_ids,
            spawned_dossier_id=r["spawned_dossier_id"]))
    return out


def findings_for(conn: sqlite3.Connection,
                 dossier_id: int) -> list[FindingInfo]:
    rows = conn.execute(
        "SELECT * FROM finding WHERE dossier_id = ? ORDER BY id",
        (dossier_id,)).fetchall()
    out: list[FindingInfo] = []
    for r in rows:
        evidence = [FindingEvidenceInfo(
            document_id=e["document_id"], quote=e["quote"],
            quote_start=e["quote_start"], quote_end=e["quote_end"],
            title=e["title"], url=e["url"], source_name=e["source_name"],
            credibility_tier=e["credibility_tier"],
        ) for e in conn.execute(
            "SELECT fe.document_id, fe.quote, fe.quote_start, fe.quote_end,"
            " d.title, d.url, s.name AS source_name, s.credibility_tier"
            " FROM finding_evidence fe"
            " JOIN document d ON d.id = fe.document_id"
            " LEFT JOIN source s ON s.id = d.source_id"
            " WHERE fe.finding_id = ? ORDER BY fe.id", (r["id"],))]
        out.append(FindingInfo(
            id=r["id"], kind=r["kind"], text=r["text"],
            speculation=bool(r["speculation"]), confidence=r["confidence"],
            question_id=r["question_id"], edge_id=r["edge_id"],
            payload=_content(r["payload"]), evidence=evidence))
    return out


def last_seq(conn: sqlite3.Connection, dossier_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(ev.seq), 0) FROM job_event ev"
        " JOIN job j ON j.id = ev.job_id WHERE j.dossier_id = ?",
        (dossier_id,)).fetchone()
    return int(row[0])


def get_detail(conn: sqlite3.Connection,
               dossier_id: int) -> InvestigationDetail | None:
    dossier = conn.execute(
        "SELECT * FROM dossier WHERE id = ? AND kind = 'investigation'",
        (dossier_id,)).fetchone()
    if dossier is None:
        return None
    section_rows = conn.execute(
        "SELECT stage, status, content, created_at, updated_at"
        " FROM dossier_section WHERE dossier_id = ?",
        (dossier_id,)).fetchall()
    stages: list[StageInfo] = []
    sections: dict[str, Any] = {}
    for row in sorted(section_rows,
                      key=lambda r: _STAGE_ORDER.get(r["stage"], 99)):
        content = _content(row["content"])
        if row["stage"] in _STAGE_ORDER:
            finished = row["updated_at"] \
                if row["status"] in ("completed", "failed") else None
            stages.append(StageInfo(
                stage=row["stage"], status=row["status"],
                summary=content.get("summary"),
                started_at=row["created_at"], finished_at=finished))
        if row["stage"] in SECTION_STAGES:
            sections[row["stage"]] = content
    usage = _content(dossier["model_usage"])
    cost = usage.get("cost_usd")
    return InvestigationDetail(
        id=dossier["id"], status=dossier["status"],
        input_text=dossier["input_text"], input_type=dossier["input_type"],
        title=dossier["title"],
        parent_question_id=dossier["parent_question_id"],
        budget_usd=dossier["budget_usd"], error=dossier["error"],
        created_at=dossier["created_at"], started_at=dossier["started_at"],
        finished_at=dossier["finished_at"], stages=stages,
        sections=sections, questions=questions_for(conn, dossier_id),
        findings=findings_for(conn, dossier_id),
        counts=counts_for(conn, dossier_id),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
        last_seq=last_seq(conn, dossier_id))
