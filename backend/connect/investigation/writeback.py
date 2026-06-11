"""record_finding — THE grounding gate of investigation mode (design §3.4).

Mechanical validation, then persistence: finding + finding_evidence rows,
an idempotent grade-2 causal edge, and the question open -> partial flip.
Every rejection raises FindingValidationError with the EXACT reason; the
tool executor surfaces it as a tool_result with is_error=True so the model
can retry once with the failure visible.

Rules enforced here:
1. every quote verbatim-verifies against the stored document text
   (the SHARED span verifier, analysis/grounding.py);
2. kind='alternative' -> evidence required, speculation forbidden;
3. speculation + reaction/trigger -> candidate_id required, from the
   closed scope-pack menu;
4. evidence=[] only when speculation=true;
5. link node ids must exist, relation must be in the causal vocabulary,
   src/dst types must match the relation signature (storage/edges.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable

from connect.analysis import grounding
from connect.domain import enums as E
from connect.investigation.schema import ReactionCandidate, ScopePack
from connect.storage import edges as edge_dao
from connect.storage.db import utc_now

log = logging.getLogger(__name__)

# node existence checks (link validation) — node_type -> table
_NODE_TABLES = {"entity": "entity", "event": "event", "claim": "claim",
                "document": "document", "dossier": "dossier"}


class FindingValidationError(Exception):
    """Rejected record_finding call; str(e) is the exact reason."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise FindingValidationError(reason)


def _node_exists(conn: sqlite3.Connection, node_type: str,
                 node_id: int) -> bool:
    table = _NODE_TABLES.get(node_type)
    if table is None:
        return False
    return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?",
                        (node_id,)).fetchone() is not None


def _validate_evidence(conn: sqlite3.Connection,
                       evidence: Any) -> list[dict[str, Any]]:
    _require(isinstance(evidence, list),
             "evidence must be a list of {document_id, quote}")
    cleaned: list[dict[str, Any]] = []
    for i, item in enumerate(evidence):
        _require(isinstance(item, dict),
                 f"evidence[{i}] must be an object with document_id and"
                 " quote")
        doc_id = item.get("document_id")
        quote = item.get("quote")
        _require(isinstance(doc_id, int),
                 f"evidence[{i}].document_id must be an integer")
        _require(isinstance(quote, str) and quote.strip() != "",
                 f"evidence[{i}].quote must be a non-empty string")
        row = conn.execute(
            "SELECT content_text FROM document WHERE id = ?",
            (doc_id,)).fetchone()
        _require(row is not None,
                 f"evidence[{i}]: document {doc_id} not found")
        content = row["content_text"] or ""
        _require(
            grounding.span_is_verbatim(quote, content),
            f"evidence[{i}]: quote is not a verbatim substring of"
            f" document {doc_id} — copy the sentence exactly as it appears")
        start, end = grounding.find_span(quote, content)
        cleaned.append({"document_id": doc_id, "quote": quote,
                        "quote_start": start, "quote_end": end})
    return cleaned


def _validate_link(conn: sqlite3.Connection,
                   link: Any) -> dict[str, Any]:
    _require(isinstance(link, dict),
             "link must be an object {src_type, src_id, relation,"
             " dst_type, dst_id}")
    for key in ("src_type", "src_id", "relation", "dst_type", "dst_id"):
        _require(key in link, f"link.{key} is required")
    relation = link["relation"]
    _require(relation in edge_dao.CAUSAL_RELATIONS,
             f"link.relation {relation!r} is not in the causal vocabulary"
             f" {edge_dao.CAUSAL_RELATIONS}")
    src_ok, dst_ok = edge_dao.CAUSAL_SIGNATURES[relation]
    _require(link["src_type"] in src_ok and link["dst_type"] in dst_ok,
             f"relation {relation!r} does not accept"
             f" {link['src_type']!r} -> {link['dst_type']!r}")
    if relation == "alternative_to":
        _require(link["src_type"] == link["dst_type"],
                 "alternative_to connects same-kind nodes")
    for side in ("src", "dst"):
        node_type, node_id = link[f"{side}_type"], link[f"{side}_id"]
        _require(isinstance(node_id, int),
                 f"link.{side}_id must be an integer")
        _require(_node_exists(conn, node_type, node_id),
                 f"link.{side}: {node_type} {node_id} does not exist")
    return dict(link)


def record_finding(conn: sqlite3.Connection, *, dossier_id: int,
                   scope_pack: ScopePack, args: dict[str, Any],
                   emit: Callable[[str, dict[str, Any]], Any] | None = None,
                   ) -> dict[str, Any]:
    """Validate + persist one finding. Returns the persisted summary dict;
    raises FindingValidationError with the exact reason on any violation."""
    kind = args.get("kind")
    _require(kind in E.FINDING_KINDS,
             f"kind must be one of {E.FINDING_KINDS}")
    text = args.get("text")
    _require(isinstance(text, str) and text.strip() != "",
             "text must be a non-empty string")
    speculation = bool(args.get("speculation", False))
    confidence = args.get("confidence")
    if confidence is not None:
        _require(isinstance(confidence, (int, float)),
                 "confidence must be a number")
        confidence = min(1.0, max(0.0, float(confidence)))

    evidence = _validate_evidence(conn, args.get("evidence", []))

    # rule 2: alternatives are mined, never invented
    if kind == "alternative":
        _require(len(evidence) > 0,
                 "kind 'alternative' requires non-empty evidence — "
                 "alternatives are mined from sources, never invented")
        _require(not speculation,
                 "kind 'alternative' forbids speculation=true")

    # rule 4: only speculation may go evidence-free
    _require(evidence or speculation,
             "evidence is required when speculation=false")

    # rule 3: speculative reaction/trigger must cite the closed menu
    candidate: ReactionCandidate | None = None
    candidate_id = args.get("candidate_id")
    if speculation and kind in ("reaction", "trigger"):
        _require(isinstance(candidate_id, str) and candidate_id != "",
                 "speculative reaction/trigger findings require a"
                 " candidate_id from the reaction-candidate menu")
        candidate = scope_pack.candidate(candidate_id)
        _require(candidate is not None,
                 f"candidate_id {candidate_id!r} is not on the"
                 " reaction-candidate menu")
    elif candidate_id is not None:
        candidate = scope_pack.candidate(str(candidate_id))
        _require(candidate is not None,
                 f"candidate_id {candidate_id!r} is not on the"
                 " reaction-candidate menu")

    # rule 5: validate the explicit link (or derive one from the candidate)
    link = args.get("link")
    if link is not None:
        link = _validate_link(conn, link)
    elif candidate is not None and kind in ("reaction", "trigger"):
        relation = "reaction_to" if kind == "reaction" else "triggered_by"
        link = {"src_type": "event", "src_id": candidate.src_event_id,
                "dst_type": "event", "dst_id": candidate.dst_event_id,
                "relation": relation}
        for side in ("src", "dst"):
            _require(_node_exists(conn, "event", link[f"{side}_id"]),
                     f"candidate {candidate.candidate_id}: event"
                     f" {link[f'{side}_id']} does not exist")

    # validate question binding (open -> partial on success)
    question_id = args.get("question_id")
    if question_id is not None:
        _require(isinstance(question_id, int),
                 "question_id must be an integer")
        row = conn.execute(
            "SELECT dossier_id FROM question WHERE id = ?",
            (question_id,)).fetchone()
        _require(row is not None, f"question {question_id} not found")
        _require(int(row["dossier_id"]) == dossier_id,
                 f"question {question_id} belongs to another dossier")

    # ---- persistence (validation passed) ------------------------------------
    payload: dict[str, Any] = {}
    if candidate is not None:
        payload["candidate_id"] = candidate.candidate_id
        payload["score_components"] = {
            "days_apart": candidate.days_apart,
            "entity_jaccard": candidate.entity_jaccard,
            "cosine": candidate.cosine,
        }
    now = utc_now()
    with conn:
        cur = conn.execute(
            "INSERT INTO finding (dossier_id, kind, text, speculation,"
            " confidence, question_id, payload, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (dossier_id, kind, text.strip(), int(speculation), confidence,
             question_id, json.dumps(payload), now))
        finding_id = int(cur.lastrowid)  # type: ignore[arg-type]
        for item in evidence:
            conn.execute(
                "INSERT INTO finding_evidence (finding_id, document_id,"
                " quote, quote_start, quote_end) VALUES (?,?,?,?,?)",
                (finding_id, item["document_id"], item["quote"],
                 item["quote_start"], item["quote_end"]))

    edge_id: int | None = None
    if link is not None:
        properties: dict[str, Any] = {
            "quote": evidence[0]["quote"] if evidence else None,
            "speculation": speculation,
            "finding_id": finding_id,
        }
        if "score_components" in payload:
            properties["score_components"] = payload["score_components"]
        edge_id = edge_dao.insert_causal(
            conn,
            src_type=link["src_type"], src_id=link["src_id"],
            dst_type=link["dst_type"], dst_id=link["dst_id"],
            relation=link["relation"], properties=properties,
            provenance_document_id=(evidence[0]["document_id"]
                                    if evidence else None),
            provenance_dossier_id=dossier_id,
            confidence=confidence, grade=2)
        with conn:
            conn.execute("UPDATE finding SET edge_id = ? WHERE id = ?",
                         (edge_id, finding_id))

    if question_id is not None:
        _attach_finding_to_question(conn, question_id, finding_id)

    result = {"finding_id": finding_id, "kind": kind,
              "speculation": speculation, "edge_id": edge_id,
              "question_id": question_id,
              "evidence_count": len(evidence)}
    if emit is not None:
        emit("finding_recorded", result)
    return result


def _attach_finding_to_question(conn: sqlite3.Connection, question_id: int,
                                finding_id: int) -> None:
    """Append the finding id and flip an 'open' question to 'partial'
    (answered/dropped statuses are left alone)."""
    row = conn.execute(
        "SELECT status, answer_finding_ids FROM question WHERE id = ?",
        (question_id,)).fetchone()
    if row is None:  # validated above; defensive
        return
    try:
        ids = json.loads(row["answer_finding_ids"] or "[]")
    except ValueError:
        ids = []
    if finding_id not in ids:
        ids.append(finding_id)
    new_status = "partial" if row["status"] == "open" else row["status"]
    with conn:
        conn.execute(
            "UPDATE question SET status = ?, answer_finding_ids = ?,"
            " updated_at = ? WHERE id = ?",
            (new_status, json.dumps(ids), utc_now(), question_id))
