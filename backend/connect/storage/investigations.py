"""Investigation (dossier kind='investigation') read DAO — assembles the
/api/investigations contracts from dossier + dossier_section + question +
finding (+ finding_evidence) rows."""

from __future__ import annotations

from typing import Any

import psycopg

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


def _content(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


async def counts_for(conn: psycopg.AsyncConnection,
                     dossier_id: int) -> InvestigationCounts:
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM finding WHERE dossier_id = %s",
        (dossier_id,))
    findings = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM question WHERE dossier_id = %s"
        " AND status IN ('open', 'partial')", (dossier_id,))
    open_q = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM question WHERE dossier_id = %s"
        " AND status = 'answered'", (dossier_id,))
    answered = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT model_usage FROM dossier WHERE id = %s", (dossier_id,))
    usage = _content((await cur.fetchone())["model_usage"])
    docs_added = usage.get("docs_added")
    return InvestigationCounts(
        findings=int(findings), questions_open=int(open_q),
        questions_answered=int(answered),
        docs_added=int(docs_added) if isinstance(docs_added, int) else 0)


# the dossier tenancy read predicate (design §5): mine OR shared
_DOSSIER_VISIBLE = "(d.owner_id = %s OR d.visibility = 'shared')"


async def list_page(conn: psycopg.AsyncConnection, *, page: int = 1,
                    page_size: int = 20,
                    viewer: int | None = None) -> InvestigationPage:
    where, params = "", []
    if viewer is not None:
        where = f" AND {_DOSSIER_VISIBLE}"
        params.append(viewer)
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM dossier d"
        " WHERE d.kind = 'investigation'" + where, params)
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT d.id, d.status, d.title, d.input_text, d.input_type,"
        " d.created_at, d.finished_at, d.visibility, d.owner_id,"
        " u.name AS owner_name"
        " FROM dossier d LEFT JOIN app_user u ON u.id = d.owner_id"
        " WHERE d.kind = 'investigation'" + where +
        " ORDER BY d.id DESC LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    items = [InvestigationListItem(
        id=r["id"], status=r["status"], title=r["title"],
        input_text=r["input_text"], input_type=r["input_type"],
        created_at=r["created_at"], finished_at=r["finished_at"],
        counts=await counts_for(conn, r["id"]),
        visibility=r["visibility"], owner_id=r["owner_id"],
        owner_name=r["owner_name"],
    ) for r in rows]
    return InvestigationPage(items=items, total=int(total), page=page,
                             page_size=page_size)


async def questions_for(conn: psycopg.AsyncConnection,
                        dossier_id: int) -> list[QuestionInfo]:
    cur = await conn.execute(
        "SELECT * FROM question WHERE dossier_id = %s ORDER BY id",
        (dossier_id,))
    out: list[QuestionInfo] = []
    for r in await cur.fetchall():
        raw_ids = r["answer_finding_ids"]
        finding_ids = ([int(f) for f in raw_ids]
                       if isinstance(raw_ids, list) else [])
        out.append(QuestionInfo(
            id=r["id"], qtype=r["qtype"], text=r["text"],
            about_type=r["about_type"], about_id=r["about_id"],
            status=r["status"], priority=r["priority"],
            answer_summary=r["answer_summary"],
            answer_finding_ids=finding_ids,
            spawned_dossier_id=r["spawned_dossier_id"]))
    return out


async def findings_for(conn: psycopg.AsyncConnection,
                       dossier_id: int, *,
                       viewer: int | None = None) -> list[FindingInfo]:
    cur = await conn.execute(
        "SELECT * FROM finding WHERE dossier_id = %s ORDER BY id",
        (dossier_id,))
    out: list[FindingInfo] = []
    vis_clause = (" AND (d.visibility = 'shared' OR d.owner_id = %s)"
                  if viewer is not None else "")
    for r in await cur.fetchall():
        params: tuple = ((r["id"], viewer) if viewer is not None
                         else (r["id"],))
        ecur = await conn.execute(
            "SELECT fe.document_id, fe.quote, fe.quote_start, fe.quote_end,"
            " d.title, d.url, s.name AS source_name, s.credibility_tier"
            " FROM finding_evidence fe"
            " JOIN document d ON d.id = fe.document_id"
            " LEFT JOIN source s ON s.id = d.source_id"
            " WHERE fe.finding_id = %s" + vis_clause + " ORDER BY fe.id",
            params)
        evidence = [FindingEvidenceInfo(
            document_id=e["document_id"], quote=e["quote"],
            quote_start=e["quote_start"], quote_end=e["quote_end"],
            title=e["title"], url=e["url"], source_name=e["source_name"],
            credibility_tier=e["credibility_tier"],
        ) for e in await ecur.fetchall()]
        out.append(FindingInfo(
            id=r["id"], kind=r["kind"], text=r["text"],
            speculation=bool(r["speculation"]), confidence=r["confidence"],
            question_id=r["question_id"], edge_id=r["edge_id"],
            payload=_content(r["payload"]), evidence=evidence))
    return out


async def last_seq(conn: psycopg.AsyncConnection, dossier_id: int) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(MAX(ev.seq), 0) AS n FROM job_event ev"
        " JOIN job j ON j.id = ev.job_id WHERE j.dossier_id = %s",
        (dossier_id,))
    return int((await cur.fetchone())["n"])


async def get_detail(conn: psycopg.AsyncConnection,
                     dossier_id: int, *,
                     viewer: int | None = None,
                     ) -> InvestigationDetail | None:
    where, params = "", [dossier_id]
    if viewer is not None:
        where = f" AND {_DOSSIER_VISIBLE}"
        params.append(viewer)
    cur = await conn.execute(
        "SELECT d.*, u.name AS owner_name"
        " FROM dossier d LEFT JOIN app_user u ON u.id = d.owner_id"
        " WHERE d.id = %s AND d.kind = 'investigation'" + where,
        params)
    dossier = await cur.fetchone()
    if dossier is None:
        return None
    cur = await conn.execute(
        "SELECT stage, status, content, created_at, updated_at"
        " FROM dossier_section WHERE dossier_id = %s",
        (dossier_id,))
    section_rows = await cur.fetchall()
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
        sections=sections, questions=await questions_for(conn, dossier_id),
        findings=await findings_for(conn, dossier_id, viewer=viewer),
        counts=await counts_for(conn, dossier_id),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
        last_seq=await last_seq(conn, dossier_id),
        visibility=dossier["visibility"], owner_id=dossier["owner_id"],
        owner_name=dossier["owner_name"])
