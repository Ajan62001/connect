"""The 10 investigation tools — frozen ToolDefs + the executor (design §2.2).

ToolDefs are plain JSON-Schema declarations the provider ships verbatim;
the executor closes over (conn, pipeline, search_client, scope_pack,
state, ...) and returns ToolOutcome(content=json, is_error=...) per call.
Validation failures (record_finding) surface as is_error tool_results with
the EXACT reason so the model can retry once with the failure visible.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import psycopg

from connect.domain import enums as E
from connect.ingestion.pipeline import IngestionPipeline
from connect.investigation.schema import ScopePack
from connect.investigation.writeback import (
    FindingValidationError,
    record_finding,
)
from connect.knowledge.embedder import Embedder
from connect.knowledge.vector import VectorIndex
from connect.llm.provider import ToolCall, ToolDef, ToolOutcome
from connect.retrieval.search import hybrid_document_ids
from connect.retrieval.search_client import SearchClient, SearchError
from connect.storage.pg import Jsonb, utc_now

log = logging.getLogger(__name__)

READ_CHUNK_CHARS = 6000
SNIPPET_CHARS = 240

_EVIDENCE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "document_id": {"type": "integer"},
            "quote": {"type": "string",
                      "description": "VERBATIM substring of the stored"
                                     " document text"},
        },
        "required": ["document_id", "quote"],
    },
    "description": "Verbatim quotes grounding the finding; [] only when"
                   " speculation=true (and kind allows)",
}

_LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "src_type": {"type": "string", "enum": ["entity", "event"]},
        "src_id": {"type": "integer"},
        "relation": {"type": "string",
                     "enum": ["reaction_to", "triggered_by", "enables",
                              "blocks", "alternative_to"]},
        "dst_type": {"type": "string", "enum": ["entity", "event"]},
        "dst_id": {"type": "integer"},
    },
    "required": ["src_type", "src_id", "relation", "dst_type", "dst_id"],
    "description": "Optional causal edge to write into the shared graph",
}

TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        name="search_corpus",
        description="Search the local document corpus (FTS + vector"
                    " fusion). Returns document ids, titles, sources,"
                    " credibility tiers, dates and snippets. ALWAYS try"
                    " this before web_search.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 8},
                "published_before": {"type": "string",
                                     "description": "ISO date filter"},
                "published_after": {"type": "string",
                                    "description": "ISO date filter"},
            },
            "required": ["query"],
        }),
    ToolDef(
        name="web_search",
        description="Search the open web (used when the corpus has fewer"
                    " than ~3 relevant documents or is single-domain)."
                    " Returns urls + snippets only; fetch_and_ingest a"
                    " result before quoting it.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        }),
    ToolDef(
        name="fetch_and_ingest",
        description="Fetch one URL and ingest it as a permanent corpus"
                    " document (deduped, indexed, T1-enriched, attached to"
                    " the 'Web (investigation)' tier-4 source). Counted"
                    " against the per-run web-fetch cap; an already-known"
                    " URL returns the existing document free.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }),
    ToolDef(
        name="read_document",
        description="Read a document's stored text (6000 chars per call;"
                    " page with offset). Quotes in record_finding must be"
                    " verbatim substrings of this text.",
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "integer"},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["document_id"],
        }),
    ToolDef(
        name="graph_neighborhood",
        description="1-hop active edges of a node (entity/event/claim/"
                    "document), grouped by relation, with provenance,"
                    " speculation flags and grades.",
        input_schema={
            "type": "object",
            "properties": {
                "node_type": {"type": "string",
                              "enum": list(E.NODE_TYPES)},
                "node_id": {"type": "integer"},
                "relations": {"type": "array",
                              "items": {"type": "string"},
                              "description": "Optional relation filter"},
            },
            "required": ["node_type", "node_id"],
        }),
    ToolDef(
        name="entity_timeline",
        description="Events an entity participated in (via document"
                    " mentions) plus claims about it with verdicts, inside"
                    " a day window ending today.",
        input_schema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "integer"},
                "name": {"type": "string",
                         "description": "Entity name (used when entity_id"
                                        " is unknown)"},
                "window_days": {"type": "integer", "default": 365},
            },
        }),
    ToolDef(
        name="calendar_context",
        description="Known calendar events (elections, budget, sessions,"
                    " RBI MPC...) near a date.",
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date"},
                "window_days": {"type": "integer", "default": 120},
            },
            "required": ["date"],
        }),
    ToolDef(
        name="record_finding",
        description="Persist one finding — THE grounding gate. Grounded"
                    " findings carry verbatim quote evidence"
                    " (speculation=false); speculative reaction/trigger"
                    " findings MUST cite a candidate_id from the closed"
                    " reaction-candidate menu. kind='alternative' requires"
                    " evidence and forbids speculation. An optional link"
                    " writes a grade-2 causal edge into the shared graph.",
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": list(E.FINDING_KINDS)},
                "text": {"type": "string"},
                "evidence": _EVIDENCE_SCHEMA,
                "speculation": {"type": "boolean", "default": False},
                "confidence": {"type": "number"},
                "question_id": {"type": "integer",
                                "description": "The question this finding"
                                               " advances"},
                "link": _LINK_SCHEMA,
                "candidate_id": {"type": "string",
                                 "description": "Reaction-candidate menu"
                                                " id (RC1...)"},
            },
            "required": ["kind", "text", "evidence"],
        }),
    ToolDef(
        name="raise_question",
        description="Raise a new open question mid-investigation (it joins"
                    " the work queue and the open-questions output).",
        input_schema={
            "type": "object",
            "properties": {
                "qtype": {"type": "string",
                          "enum": list(E.QUESTION_TYPES)},
                "text": {"type": "string"},
                "about": {
                    "type": "object",
                    "properties": {
                        "about_type": {"type": "string",
                                       "enum": list(E.NODE_TYPES)},
                        "about_id": {"type": "integer"},
                    },
                    "required": ["about_type", "about_id"],
                },
                "priority": {"type": "number", "default": 0.5},
            },
            "required": ["qtype", "text"],
        }),
    ToolDef(
        name="conclude",
        description="TERMINAL — end the investigation. Resolve EVERY open"
                    " question honestly (answered/partial/open with the"
                    " finding ids that support the resolution) and give a"
                    " short overall summary + confidence.",
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "resolutions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question_id": {"type": "integer"},
                            "status": {"type": "string",
                                       "enum": ["answered", "partial",
                                                "open"]},
                            "answer_summary": {"type": "string"},
                            "finding_ids": {"type": "array",
                                            "items": {"type": "integer"}},
                        },
                        "required": ["question_id", "status"],
                    },
                },
                "confidence": {"type": "number"},
            },
            "required": ["summary", "resolutions", "confidence"],
        }),
)

TOOL_NAMES = tuple(t.name for t in TOOL_DEFS)


# -- state + executor ---------------------------------------------------------------


@dataclass
class InvestigationState:
    """Mutable per-run state shared between the runner and the executor."""
    dossier_id: int
    max_web_fetches: int = 8
    web_fetches_used: int = 0
    corpus_only: bool = False          # degrade-ladder 60% flip
    concluded: bool = False
    conclude_summary: str | None = None
    conclude_confidence: float | None = None
    docs_added: int = 0
    findings_recorded: int = 0
    questions_raised: int = 0
    rejected_findings: int = 0
    touched_doc_ids: set[int] = field(default_factory=set)


class ToolExecutor:
    """Dispatches one ToolCall; every failure becomes an is_error outcome,
    never an exception into the loop."""

    def __init__(self, conn: psycopg.AsyncConnection, *,
                 pipeline: IngestionPipeline | None,
                 search: SearchClient,
                 embedder: Embedder,
                 vectors: VectorIndex | None,
                 scope_pack: ScopePack,
                 state: InvestigationState,
                 emit: Callable[[str, dict[str, Any]], Awaitable[Any]],
                 web_source_id: int | None = None,
                 t1_enrich: Callable[[int], Awaitable[bool]] | None = None):
        self.conn = conn
        self.pipeline = pipeline
        self.search = search
        self.embedder = embedder
        self.vectors = vectors
        self.scope_pack = scope_pack
        self.state = state
        self.emit = emit
        self.web_source_id = web_source_id
        self.t1_enrich = t1_enrich

    async def __call__(self, call: ToolCall) -> ToolOutcome:
        handler = getattr(self, f"_tool_{call.name}", None)
        if handler is None:
            return ToolOutcome(
                content=f"unknown tool {call.name!r}", is_error=True)
        try:
            result = await handler(dict(call.input))
        except FindingValidationError as e:
            self.state.rejected_findings += 1
            return ToolOutcome(content=f"rejected: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 — tool failure is data
            log.exception("tool %s failed", call.name)
            return ToolOutcome(content=f"{call.name} failed: {e}",
                               is_error=True)
        if isinstance(result, ToolOutcome):
            return result
        return ToolOutcome(content=json.dumps(result))

    # -- corpus / web ------------------------------------------------------------

    async def _tool_search_corpus(self, args: dict[str, Any]) -> Any:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolOutcome(content="query is required", is_error=True)
        top_k = max(1, min(int(args.get("top_k", 8) or 8), 20))
        before = args.get("published_before")
        after = args.get("published_after")

        # the shared hybrid path (retrieval layer): lexical + vector under
        # RRF; either leg degrades to empty, fusion of one list is the list
        fused = await hybrid_document_ids(
            self.conn, query, embedder=self.embedder, vectors=self.vectors,
            lexical_k=top_k * 2, vector_k=top_k * 2)

        results: list[dict[str, Any]] = []
        for doc_id in fused:
            cur = await self.conn.execute(
                "SELECT d.id, d.title, d.published_at, d.content_text,"
                " s.name AS source_name, s.credibility_tier"
                " FROM document d LEFT JOIN source s ON s.id = d.source_id"
                " WHERE d.id = %s", (doc_id,))
            row = await cur.fetchone()
            if row is None:
                continue
            published = row["published_at"]
            if before and published and published[:10] >= str(before)[:10]:
                continue
            if after and published and published[:10] <= str(after)[:10]:
                continue
            self.state.touched_doc_ids.add(row["id"])
            results.append({
                "document_id": row["id"],
                "title": row["title"],
                "source": row["source_name"],
                "tier": row["credibility_tier"],
                "published_at": published,
                "snippet": (row["content_text"] or "")[:SNIPPET_CHARS],
            })
            if len(results) >= top_k:
                break
        return {"results": results, "total": len(results)}

    async def _tool_web_search(self, args: dict[str, Any]) -> Any:
        if self.state.corpus_only:
            return ToolOutcome(
                content="web search disabled: budget degraded to"
                        " corpus-only — use search_corpus",
                is_error=True)
        if self.search.name == "null":
            return ToolOutcome(
                content="web search unavailable; corpus only",
                is_error=True)
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolOutcome(content="query is required", is_error=True)
        max_results = max(1, min(int(args.get("max_results", 5) or 5), 10))
        try:
            hits = await self.search.search(query, max_results=max_results)
        except SearchError as e:
            return ToolOutcome(content=f"web search failed: {e}",
                               is_error=True)
        return {"results": [
            {"url": h.url, "title": h.title, "snippet": h.snippet}
            for h in hits]}

    async def _tool_fetch_and_ingest(self, args: dict[str, Any]) -> Any:
        if self.state.corpus_only:
            return ToolOutcome(
                content="web fetch disabled: budget degraded to"
                        " corpus-only", is_error=True)
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolOutcome(content="url is required", is_error=True)
        cur = await self.conn.execute(
            "SELECT id, title FROM document WHERE url = %s"
            " OR canonical_url = %s LIMIT 1", (url, url))
        existing = await cur.fetchone()
        if existing is not None:
            self.state.touched_doc_ids.add(existing["id"])
            return {"document_id": existing["id"],
                    "title": existing["title"], "created": False,
                    "note": "url already in corpus (no fetch spent)"}
        if self.state.web_fetches_used >= self.state.max_web_fetches:
            return ToolOutcome(
                content=f"web fetch cap reached"
                        f" ({self.state.max_web_fetches} per run)",
                is_error=True)
        if self.pipeline is None:
            return ToolOutcome(content="ingestion pipeline unavailable",
                               is_error=True)
        result = await self.pipeline.ingest_url(
            self.conn, url, source_id=self.web_source_id)
        self.state.web_fetches_used += 1
        doc = result.document
        self.state.touched_doc_ids.add(doc.id)
        t1_done = False
        if result.created:
            self.state.docs_added += 1
            await self.emit("doc_ingested", {"document_id": doc.id,
                                             "url": url,
                                             "title": doc.title})
            if self.t1_enrich is not None:
                t1_done = await self.t1_enrich(doc.id)
        return {"document_id": doc.id, "title": doc.title,
                "created": result.created, "t1_enriched": t1_done,
                "chars": len(doc.content_text or "")}

    # -- reads --------------------------------------------------------------------

    async def _tool_read_document(self, args: dict[str, Any]) -> Any:
        doc_id = args.get("document_id")
        if not isinstance(doc_id, int):
            return ToolOutcome(content="document_id must be an integer",
                               is_error=True)
        offset = max(0, int(args.get("offset", 0) or 0))
        cur = await self.conn.execute(
            "SELECT d.id, d.title, d.url, d.published_at, d.content_text,"
            " s.name AS source_name, s.credibility_tier"
            " FROM document d LEFT JOIN source s ON s.id = d.source_id"
            " WHERE d.id = %s", (doc_id,))
        row = await cur.fetchone()
        if row is None:
            return ToolOutcome(content=f"document {doc_id} not found",
                               is_error=True)
        content = row["content_text"] or ""
        chunk = content[offset:offset + READ_CHUNK_CHARS]
        self.state.touched_doc_ids.add(doc_id)
        return {
            "document_id": row["id"], "title": row["title"],
            "source": row["source_name"], "tier": row["credibility_tier"],
            "published_at": row["published_at"], "url": row["url"],
            "offset": offset, "text": chunk,
            "has_more": offset + READ_CHUNK_CHARS < len(content),
            "total_chars": len(content),
        }

    async def _tool_graph_neighborhood(self, args: dict[str, Any]) -> Any:
        node_type = args.get("node_type")
        node_id = args.get("node_id")
        if node_type not in E.NODE_TYPES or not isinstance(node_id, int):
            return ToolOutcome(
                content="node_type must be one of"
                        f" {E.NODE_TYPES} and node_id an integer",
                is_error=True)
        relations = args.get("relations")
        sql = ("SELECT id, src_type, src_id, dst_type, dst_id, relation,"
               " properties, provenance_document_id, confidence, grade"
               " FROM edge WHERE status = 'active' AND"
               " ((src_type = %s AND src_id = %s)"
               "  OR (dst_type = %s AND dst_id = %s))")
        params: list[Any] = [node_type, node_id, node_type, node_id]
        if relations:
            sql += " AND relation = ANY(%s)"
            params.append(list(relations))
        grouped: dict[str, list[dict[str, Any]]] = {}
        cur = await self.conn.execute(sql + " ORDER BY id DESC LIMIT 60",
                                      params)
        for row in await cur.fetchall():
            outgoing = (row["src_type"] == node_type
                        and row["src_id"] == node_id)
            other_type = row["dst_type"] if outgoing else row["src_type"]
            other_id = row["dst_id"] if outgoing else row["src_id"]
            properties = (row["properties"]
                          if isinstance(row["properties"], dict) else {})
            speculation = bool(properties.get("speculation"))
            grouped.setdefault(row["relation"], []).append({
                "edge_id": row["id"],
                "direction": "out" if outgoing else "in",
                "other_type": other_type, "other_id": other_id,
                "other_title": await self._node_title(other_type, other_id),
                "speculation": speculation,
                "provenance_document_id": row["provenance_document_id"],
                "confidence": row["confidence"], "grade": row["grade"],
            })
        return {"node": {"type": node_type, "id": node_id,
                         "title": await self._node_title(node_type,
                                                         node_id)},
                "relations": grouped}

    async def _node_title(self, node_type: str,
                          node_id: int) -> str | None:
        sql = {"entity": "SELECT name AS t FROM entity WHERE id = %s",
               "event": "SELECT title AS t FROM event WHERE id = %s",
               "document": "SELECT title AS t FROM document WHERE id = %s",
               "claim": "SELECT text AS t FROM claim WHERE id = %s",
               "dossier": "SELECT input_text AS t FROM dossier"
                          " WHERE id = %s",
               }.get(node_type)
        if sql is None:
            return None
        cur = await self.conn.execute(sql, (node_id,))
        row = await cur.fetchone()
        return (row["t"][:120] if row and row["t"] else None)

    async def _tool_entity_timeline(self, args: dict[str, Any]) -> Any:
        entity_id = args.get("entity_id")
        if not isinstance(entity_id, int):
            name = str(args.get("name", "")).strip()
            if not name:
                return ToolOutcome(
                    content="entity_id or name is required", is_error=True)
            cur = await self.conn.execute(
                "SELECT id FROM entity WHERE lower(name) = lower(%s)"
                " LIMIT 1", (name,))
            row = await cur.fetchone()
            if row is None:
                cur = await self.conn.execute(
                    "SELECT id FROM entity WHERE name ILIKE %s LIMIT 1",
                    (f"%{name}%",))
                row = await cur.fetchone()
            if row is None:
                return ToolOutcome(content=f"entity {name!r} not found",
                                   is_error=True)
            entity_id = int(row["id"])
        window = max(1, min(int(args.get("window_days", 365) or 365), 3650))
        cur = await self.conn.execute(
            "SELECT id, name, entity_type FROM entity WHERE id = %s",
            (entity_id,))
        ent = await cur.fetchone()
        if ent is None:
            return ToolOutcome(content=f"entity {entity_id} not found",
                               is_error=True)
        cur = await self.conn.execute(
            "SELECT DISTINCT e.id, e.title, e.event_type, e.occurred_on,"
            " e.doc_count FROM event e"
            " JOIN event_assignment ea ON ea.event_id = e.id"
            " JOIN entity_mention m ON m.document_id = ea.document_id"
            " WHERE m.entity_id = %s AND (e.occurred_on IS NULL OR"
            " e.occurred_on >= (now() AT TIME ZONE 'utc')::date - %s)"
            " ORDER BY e.occurred_on", (entity_id, window))
        events = await cur.fetchall()
        cur = await self.conn.execute(
            "SELECT DISTINCT c.id, c.text, c.verdict, c.confidence"
            " FROM claim c JOIN document d ON d.id = c.first_document_id"
            " JOIN entity_mention m ON m.document_id = d.id"
            " WHERE m.entity_id = %s ORDER BY c.id DESC LIMIT 20",
            (entity_id,))
        claims = await cur.fetchall()
        return {
            "entity": {"entity_id": ent["id"], "name": ent["name"],
                       "entity_type": ent["entity_type"]},
            "events": [{"event_id": e["id"], "title": e["title"],
                        "event_type": e["event_type"],
                        "occurred_on": e["occurred_on"],
                        "doc_count": e["doc_count"]} for e in events],
            "claims": [{"claim_id": c["id"], "text": c["text"],
                        "verdict": c["verdict"],
                        "confidence": c["confidence"]} for c in claims],
        }

    async def _tool_calendar_context(self, args: dict[str, Any]) -> Any:
        date = str(args.get("date", "")).strip()
        if not date:
            return ToolOutcome(content="date is required", is_error=True)
        window = max(1, min(int(args.get("window_days", 120) or 120), 730))
        cur = await self.conn.execute(
            "SELECT id, kind, scope, occurs_on, ends_on, label"
            " FROM calendar_event"
            " WHERE ABS(occurs_on - %s::date) <= %s"
            " ORDER BY occurs_on", (date[:10], window))
        rows = await cur.fetchall()
        return {"items": [
            {"calendar_event_id": r["id"], "kind": r["kind"],
             "scope": r["scope"], "occurs_on": r["occurs_on"],
             "ends_on": r["ends_on"], "label": r["label"]} for r in rows]}

    # -- writes -------------------------------------------------------------------

    async def _tool_record_finding(self, args: dict[str, Any]) -> Any:
        result = await record_finding(
            self.conn, dossier_id=self.state.dossier_id,
            scope_pack=self.scope_pack, args=args, emit=self.emit)
        self.state.findings_recorded += 1
        return result

    async def _tool_raise_question(self, args: dict[str, Any]) -> Any:
        qtype = args.get("qtype")
        if qtype not in E.QUESTION_TYPES:
            return ToolOutcome(
                content=f"qtype must be one of {E.QUESTION_TYPES}",
                is_error=True)
        text = str(args.get("text", "")).strip()
        if not text:
            return ToolOutcome(content="text is required", is_error=True)
        about = args.get("about") or {}
        about_type = about.get("about_type")
        about_id = about.get("about_id")
        if about_type is not None and about_type not in E.NODE_TYPES:
            return ToolOutcome(
                content=f"about_type must be one of {E.NODE_TYPES}",
                is_error=True)
        priority = args.get("priority", 0.5)
        try:
            priority = min(1.0, max(0.0, float(priority)))
        except (TypeError, ValueError):
            priority = 0.5
        async with self.conn.transaction():
            cur = await self.conn.execute(
                "INSERT INTO question (dossier_id, qtype, text, about_type,"
                " about_id, status, priority, created_at)"
                " VALUES (%s,%s,%s,%s,%s, 'open', %s, %s) RETURNING id",
                (self.state.dossier_id, qtype, text, about_type, about_id,
                 priority, utc_now()))
            question_id = int((await cur.fetchone())["id"])
        self.state.questions_raised += 1
        await self.emit("question_raised", {"question_id": question_id,
                                            "qtype": qtype, "text": text})
        return {"question_id": question_id}

    async def _tool_conclude(self, args: dict[str, Any]) -> Any:
        summary = str(args.get("summary", "")).strip()
        confidence = args.get("confidence")
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
        resolved: list[dict[str, Any]] = []
        for res in args.get("resolutions") or []:
            if not isinstance(res, dict):
                continue
            question_id = res.get("question_id")
            status = res.get("status")
            if not isinstance(question_id, int) or status not in (
                    "answered", "partial", "open"):
                continue
            cur = await self.conn.execute(
                "SELECT id FROM question WHERE id = %s AND dossier_id = %s",
                (question_id, self.state.dossier_id))
            if await cur.fetchone() is None:
                continue
            finding_ids = []
            for f in (res.get("finding_ids") or []):
                if not isinstance(f, int):
                    continue
                cur = await self.conn.execute(
                    "SELECT 1 FROM finding WHERE id = %s", (f,))
                if await cur.fetchone() is not None:
                    finding_ids.append(f)
            if status == "answered" and not finding_ids:
                status = "partial"  # honest: answered needs findings
            async with self.conn.transaction():
                await self.conn.execute(
                    "UPDATE question SET status = %s, answer_summary = %s,"
                    " answer_finding_ids = %s, updated_at = %s"
                    " WHERE id = %s",
                    (status, res.get("answer_summary"),
                     Jsonb(finding_ids), utc_now(), question_id))
            await self.emit("question_resolved",
                            {"question_id": question_id, "status": status})
            resolved.append({"question_id": question_id, "status": status})
        self.state.concluded = True
        self.state.conclude_summary = summary
        self.state.conclude_confidence = confidence
        return {"ok": True, "resolved": resolved}
