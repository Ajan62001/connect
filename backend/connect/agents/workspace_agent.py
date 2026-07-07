"""Workspace-manager agent — a bounded, governed tool-loop chat agent scoped
to ONE workspace.

The user chats with the agent; it grounds answers in the workspace's documents
(the workspace's focus lens over the shared corpus), reads them, and can take
ONE action — post a finding tagged to the workspace. It runs synchronously
inside the request: a hand-rolled loop (like the investigation runner, minus
the SSE/cancel/job machinery) so every turn is metered (spend.record_call,
purpose='workspace_agent') and gated (general TenantGovernor + a per-turn USD
cap). Tenancy: every corpus read carries the viewer predicate; every search/
read is additionally scoped to the workspace focus.

Conversation is CLIENT-HELD: the endpoint is stateless — the request carries
the full provider-shaped ``messages`` and the response echoes the updated
array (raw_content + tool_result blocks preserved for cache-exact replay).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from connect.agents.metered_loop import run_metered_tool_loop
from connect.analysis import grounding
from connect.analysis.budget import AnalysisBudget
from connect.domain.models import Post, PostSettings, SocialPost, Workspace
from connect.llm import spend
from connect.llm.provider import (
    LLMError,
    LLMProvider,
    ToolCall,
    ToolDef,
    ToolOutcome,
)
from connect.llm.spend import BudgetExceeded
from connect.llm.tiers import ModelTier
from connect.retrieval.search import hybrid_document_ids
from connect.social import generate
from connect.social.card import render_card
from connect.social.palettes import resolve_post_theme
from connect.social.settings import load_logo
from connect.storage import posts as post_dao
from connect.storage import social_drafts as social_draft_dao
from connect.storage.workspaces import topic_focus_clauses

log = logging.getLogger(__name__)

PURPOSE_WORKSPACE_AGENT = "workspace_agent"

WS_AGENT_MAX_ITERS = 6
WS_AGENT_REQUEST_CAP_USD = 0.10
WS_AGENT_DEEP_MAX_ITERS = 20
WS_AGENT_DEEP_CAP_USD = 1.00

# The two budget presets the workspace agent runs under: a synchronous,
# in-request "quick" chat turn, and an async "deep" task (a background job)
# with higher iteration + spend caps. The (max_iters, cap_usd) pair is the
# ONLY difference between the two modes — same engine, same tools, same gate.
WS_AGENT_MODES: dict[str, tuple[int, float]] = {
    "quick": (WS_AGENT_MAX_ITERS, WS_AGENT_REQUEST_CAP_USD),
    "deep": (WS_AGENT_DEEP_MAX_ITERS, WS_AGENT_DEEP_CAP_USD),
}

_EST_IN, _EST_OUT = 6000, 700          # conservative per-turn projection
READ_CHUNK_CHARS = 6000
SNIPPET_CHARS = 240

WS_AGENT_SYSTEM = (
    "You are the helpful, conversational assistant for a workspace — a focused"
    " lens over a news corpus plus the documents added to it. Be proactive: ACT"
    " on the user's request instead of asking them to clarify or to name a"
    " document. For a broad or vague ask (a summary, synopsis, overview,"
    " 'what's new', 'what's in here', 'catch me up'), DON'T ask 'of what?' —"
    " call list_recent (and search_workspace with relevant terms) to survey the"
    " workspace, read the most relevant documents, and synthesize the answer"
    " yourself. Tools: list_recent (survey recent docs, no query), search_workspace"
    " (find specific things), read_document (read full text), post_finding (save"
    " an insight to the workspace), draft_social_post (make an Instagram post"
    " from a document). Ground every factual claim in documents you actually"
    " read (cite document ids); never invent facts or figures. Only if the"
    " workspace genuinely has NO documents, say so plainly and describe what the"
    " workspace is for. When you are ready to reply, call final_answer with your"
    " complete answer. Be concise, specific, and friendly.")


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "top_k": {"type": "integer", "default": 8},
        "published_before": {"type": "string", "description": "ISO date"},
        "published_after": {"type": "string", "description": "ISO date"},
    },
    "required": ["query"],
}

WS_TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        name="list_recent",
        description="List the most recent documents in this workspace (NO query"
                    " needed). Use this to survey the workspace for an overview,"
                    " summary, synopsis, or 'what's new' — then read the ones"
                    " worth reading. Returns document ids, titles, sources,"
                    " dates and snippets, newest first.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
            },
        }),
    ToolDef(
        name="search_workspace",
        description="Search this workspace's documents (FTS + vector fusion,"
                    " scoped to the workspace's focus). Returns document ids,"
                    " titles, sources, dates and snippets. Use this first.",
        input_schema=_SEARCH_SCHEMA),
    ToolDef(
        name="read_document",
        description="Read a workspace document's stored text (6000 chars per"
                    " call; page with offset). Only documents in this"
                    " workspace are readable.",
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "integer"},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["document_id"],
        }),
    ToolDef(
        name="post_finding",
        description="Save a finding/insight to this workspace (a short titled"
                    " note). MUST cite the workspace document it came from and"
                    " a quote: an EXACT, verbatim sentence copied from that"
                    " document's text that backs the finding (copy it"
                    " character-for-character — it is mechanically verified"
                    " against the stored document and rejected if it is not a"
                    " literal substring). Use when the user asks to capture"
                    " something.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "document_id": {"type": "integer",
                                "description": "The cited workspace document"},
                "quote": {"type": "string",
                          "description": "A verbatim sentence from the cited"
                                         " document that grounds the finding"},
            },
            "required": ["title", "body", "document_id", "quote"],
        }),
    ToolDef(
        name="draft_social_post",
        description="Draft an Instagram post (caption + hashtags + a rendered"
                    " image card) from a workspace document, in the workspace's"
                    " post style. Saves it to the workspace's drafts. Use when"
                    " the user asks to make/draft/create a post.",
        input_schema={
            "type": "object",
            "properties": {"document_id": {"type": "integer"}},
            "required": ["document_id"],
        }),
    ToolDef(
        name="final_answer",
        description="TERMINAL — give your complete reply to the user.",
        input_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }),
)


def _build_focus(workspace: Workspace) -> tuple[str | None, list[Any]]:
    """The workspace's focus as an OR-clause + params (mirrors
    workspaces.focused_feed). None when the workspace has no focus set —
    then the agent sees the whole visible corpus."""
    clauses: list[str] = []
    params: list[Any] = []
    if workspace.source_ids:
        clauses.append("d.source_id = ANY(%s)")
        params.append([int(s) for s in workspace.source_ids])
    if workspace.topics:
        tclauses, tparams = topic_focus_clauses(workspace.topics)
        clauses.extend(tclauses)
        params.extend(tparams)
    if workspace.query_fts:
        clauses.append("d.search_tsv @@ websearch_to_tsquery('english', %s)")
        params.append(workspace.query_fts)
    if not clauses:
        return None, []   # no focus -> the whole visible corpus is in scope
    # the workspace's OWN knowledge-base docs are always in scope alongside the
    # shared-corpus lens (design: own docs + shared lens).
    clauses.append("d.workspace_id = %s")
    params.append(workspace.id)
    return "(" + " OR ".join(clauses) + ")", params


@dataclass
class WorkspaceAgentState:
    concluded: bool = False
    final_answer: str | None = None
    finding: Post | None = None
    drafts: list[int] = field(default_factory=list)   # social_draft ids made
    touched_doc_ids: set[int] = field(default_factory=set)
    tools_used: list[str] = field(default_factory=list)


class WorkspaceToolExecutor:
    """Dispatches one ToolCall; every failure becomes an is_error outcome,
    never an exception into the loop. All reads carry the viewer predicate AND
    the workspace focus clause."""

    def __init__(self, conn: psycopg.AsyncConnection, *, embedder: Any,
                 vectors: Any, workspace: Workspace, viewer: int,
                 state: WorkspaceAgentState, llm: LLMProvider | None = None,
                 governor: Any = None, card_store: Any = None,
                 logo_store: Any = None,
                 post_settings: PostSettings | None = None):
        self.conn = conn
        self.embedder = embedder
        self.vectors = vectors
        self.workspace = workspace
        self.viewer = viewer
        self.state = state
        # for the draft_social_post tool (a nested governed LLM call):
        self.llm = llm
        self.governor = governor
        self.card_store = card_store
        self.logo_store = logo_store
        self.post_settings = post_settings
        self.focus_clause, self.focus_params = _build_focus(workspace)

    async def __call__(self, call: ToolCall) -> ToolOutcome:
        handler = getattr(self, f"_tool_{call.name}", None)
        if handler is None:
            return ToolOutcome(content=f"unknown tool {call.name!r}",
                               is_error=True)
        try:
            result = await handler(dict(call.input))
        except Exception as e:  # noqa: BLE001 — tool failure is data
            log.exception("workspace tool %s failed", call.name)
            return ToolOutcome(content=f"{call.name} failed: {e}",
                               is_error=True)
        if isinstance(result, ToolOutcome):
            return result
        return ToolOutcome(content=json.dumps(result))

    async def _fetch_in_scope(self, doc_id: int) -> Any:
        sql = ("SELECT d.id, d.title, d.url, d.published_at, d.content_text,"
               " s.name AS source_name, s.credibility_tier"
               " FROM document d LEFT JOIN source s ON s.id = d.source_id"
               " WHERE d.id = %s AND (d.visibility = 'shared'"
               " OR d.owner_id = %s)")
        params: list[Any] = [doc_id, self.viewer]
        if self.focus_clause:
            sql += " AND " + self.focus_clause
            params += self.focus_params
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def _tool_list_recent(self, args: dict[str, Any]) -> Any:
        limit = max(1, min(int(args.get("limit", 10) or 10), 25))
        sql = ("SELECT d.id, d.title, d.published_at, d.content_text,"
               " s.name AS source_name, s.credibility_tier"
               " FROM document d LEFT JOIN source s ON s.id = d.source_id"
               " WHERE (d.visibility = 'shared' OR d.owner_id = %s)")
        params: list[Any] = [self.viewer]
        if self.focus_clause:
            sql += " AND " + self.focus_clause
            params += self.focus_params
        sql += " ORDER BY d.fetched_at DESC, d.id DESC LIMIT %s"
        params.append(limit)
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            self.state.touched_doc_ids.add(row["id"])
            results.append({
                "document_id": row["id"], "title": row["title"],
                "source": row["source_name"], "tier": row["credibility_tier"],
                "published_at": row["published_at"],
                "snippet": (row["content_text"] or "")[:SNIPPET_CHARS],
            })
        return {"results": results, "total": len(results)}

    async def _tool_search_workspace(self, args: dict[str, Any]) -> Any:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolOutcome(content="query is required", is_error=True)
        top_k = max(1, min(int(args.get("top_k", 8) or 8), 20))
        before = args.get("published_before")
        after = args.get("published_after")
        fused = await hybrid_document_ids(
            self.conn, query, embedder=self.embedder, vectors=self.vectors,
            lexical_k=top_k * 2, vector_k=top_k * 2, viewer=self.viewer)
        results: list[dict[str, Any]] = []
        for doc_id in fused:
            row = await self._fetch_in_scope(doc_id)
            if row is None:
                continue
            published = row["published_at"]
            if before and published and published[:10] >= str(before)[:10]:
                continue
            if after and published and published[:10] <= str(after)[:10]:
                continue
            self.state.touched_doc_ids.add(row["id"])
            results.append({
                "document_id": row["id"], "title": row["title"],
                "source": row["source_name"], "tier": row["credibility_tier"],
                "published_at": published,
                "snippet": (row["content_text"] or "")[:SNIPPET_CHARS],
            })
            if len(results) >= top_k:
                break
        return {"results": results, "total": len(results)}

    async def _tool_read_document(self, args: dict[str, Any]) -> Any:
        doc_id = args.get("document_id")
        if not isinstance(doc_id, int):
            return ToolOutcome(content="document_id must be an integer",
                               is_error=True)
        row = await self._fetch_in_scope(doc_id)
        if row is None:
            return ToolOutcome(
                content=f"document {doc_id} is not in this workspace",
                is_error=True)
        offset = max(0, int(args.get("offset", 0) or 0))
        content = row["content_text"] or ""
        chunk = content[offset:offset + READ_CHUNK_CHARS]
        self.state.touched_doc_ids.add(doc_id)
        return {
            "document_id": row["id"], "title": row["title"],
            "source": row["source_name"], "url": row["url"],
            "published_at": row["published_at"], "offset": offset,
            "text": chunk,
            "has_more": offset + READ_CHUNK_CHARS < len(content),
            "total_chars": len(content),
        }

    async def _tool_post_finding(self, args: dict[str, Any]) -> Any:
        title = str(args.get("title", "")).strip()
        body = str(args.get("body", "")).strip()
        if not title or not body:
            return ToolOutcome(content="title and body are required",
                               is_error=True)
        # grounding gate (mirrors investigation/writeback.py): a finding must
        # cite a workspace document AND a verbatim quote that literally appears
        # in it — agent claims are mechanically grounded, never prompt-hoped.
        doc_id = args.get("document_id")
        if not isinstance(doc_id, int):
            return ToolOutcome(content="document_id must be an integer",
                               is_error=True)
        row = await self._fetch_in_scope(doc_id)
        if row is None:
            return ToolOutcome(
                content=f"document {doc_id} is not in this workspace",
                is_error=True)
        quote = str(args.get("quote", "")).strip()
        if not quote:
            return ToolOutcome(
                content="quote is required — copy a verbatim sentence from the"
                        " cited document that backs this finding",
                is_error=True)
        content = row["content_text"] or ""
        if not grounding.span_is_verbatim(quote, content):
            return ToolOutcome(
                content=f"quote is not a verbatim substring of document"
                        f" {doc_id} — copy the sentence exactly as it appears",
                is_error=True)
        start, end = grounding.find_span(quote, content)
        post = await post_dao.insert(
            self.conn, owner_id=self.viewer, title=title[:300],
            body=body[:20_000], document_id=doc_id,
            workspace_id=self.workspace.id, quote=quote,
            quote_start=start, quote_end=end, visibility="shared")
        self.state.finding = post
        return {"ok": True, "post_id": post.id, "title": post.title}

    async def _tool_draft_social_post(self, args: dict[str, Any]) -> Any:
        if (self.llm is None or self.card_store is None
                or self.post_settings is None):
            return ToolOutcome(content="post drafting is unavailable",
                               is_error=True)
        doc_id = args.get("document_id")
        if not isinstance(doc_id, int):
            return ToolOutcome(content="document_id must be an integer",
                               is_error=True)
        row = await self._fetch_in_scope(doc_id)
        if row is None:
            return ToolOutcome(
                content=f"document {doc_id} is not in this workspace",
                is_error=True)
        s = self.post_settings
        system = generate.system_for(s)
        user = generate.build_prompt(
            title=row["title"], source_name=row["source_name"],
            content_text=row["content_text"])
        proj = spend.cost_usd(self.llm.model_for(ModelTier.BALANCED),
                              input_tokens=2000, output_tokens=600)
        try:
            if self.governor is not None:
                await self.governor.check(proj, user_id=self.viewer)
        except BudgetExceeded:
            return ToolOutcome(
                content="daily budget reached; could not draft a post",
                is_error=True)
        try:
            completion = await self.llm.complete_structured(
                system=system,
                messages=[{"role": "user", "content": user}],
                schema=SocialPost, tier=ModelTier.BALANCED, max_tokens=800)
        except LLMError as e:
            return ToolOutcome(content=f"draft failed: {e}", is_error=True)
        await spend.record_call(self.conn, purpose="social_post",
                                model=completion.model,
                                usage=completion.usage, user_id=self.viewer)
        content = completion.output
        themed, palette = resolve_post_theme(
            s, suggested=content.suggested_palette)
        if palette:
            content = content.model_copy(update={"suggested_palette": palette})
        logo = load_logo(self.logo_store, themed)
        image = render_card(content, settings=themed, logo=logo)
        rel = self.card_store.put(image)
        sha = rel.rsplit("/", 1)[-1]
        draft_id = await social_draft_dao.insert(
            self.conn, workspace_id=self.workspace.id, owner_id=self.viewer,
            document_id=doc_id, content=content.model_dump(), card_sha=sha)
        self.state.drafts.append(draft_id)
        return {"draft_id": draft_id, "headline": content.headline,
                "caption": content.caption, "hashtags": content.hashtags}

    async def _tool_final_answer(self, args: dict[str, Any]) -> Any:
        self.state.concluded = True
        self.state.final_answer = str(args.get("answer", "")).strip()
        return {"ok": True}


@dataclass
class WorkspaceAgentResult:
    final_answer: str
    messages: list[dict[str, Any]]
    tools_used: list[str]
    finding: Post | None
    drafts: list[int]
    turns_completed: int
    spent_usd: float
    budget_remaining_usd: float


async def run_workspace_agent(
        conn: psycopg.AsyncConnection, *, llm: LLMProvider, governor: Any,
        embedder: Any, vectors: Any, workspace: Workspace, viewer: int,
        messages: list[dict[str, Any]],
        card_store: Any = None, logo_store: Any = None,
        post_settings: PostSettings | None = None,
        character: Any = None,
        on_turn: Any = None, should_cancel: Any = None,
        mode: str = "quick") -> WorkspaceAgentResult:
    """Drive the bounded tool-loop for one user turn. ``mode`` selects the
    (max_iters, cap_usd) preset — 'quick' for the in-request chat, 'deep' for
    the async task; the loop is otherwise identical. Raises BudgetExceeded
    only when the FIRST turn is refused (nothing spent → the endpoint 429s);
    a mid-loop budget hit forces one final answer and returns a partial."""
    max_iters, cap_usd = WS_AGENT_MODES.get(mode, WS_AGENT_MODES["quick"])
    state = WorkspaceAgentState()
    executor = WorkspaceToolExecutor(
        conn, embedder=embedder, vectors=vectors, workspace=workspace,
        viewer=viewer, state=state, llm=llm, governor=governor,
        card_store=card_store, logo_store=logo_store,
        post_settings=post_settings)
    budget = AnalysisBudget(cap_usd=cap_usd)
    model = llm.model_for(ModelTier.BALANCED)
    turn_proj = spend.cost_usd(model, input_tokens=_EST_IN,
                               output_tokens=_EST_OUT)
    msgs = list(messages)

    async def should_force(n: int) -> bool:
        if n == max_iters or not budget.fits(turn_proj):
            return True
        try:
            await governor.check(turn_proj, user_id=viewer)
        except BudgetExceeded:
            if n == 1:
                raise              # nothing spent yet → endpoint returns 429
            return True            # one last forced turn, then stop
        return False

    async def after_turn(n: int, turn: Any, forced: bool) -> None:
        await spend.record_call(conn, purpose=PURPOSE_WORKSPACE_AGENT,
                                model=turn.model, usage=turn.usage,
                                user_id=viewer)
        budget.add(spend.cost_usd(
            turn.model, input_tokens=turn.usage.input_tokens,
            output_tokens=turn.usage.output_tokens,
            cache_read_tokens=turn.usage.cache_read_tokens))
        names = [c.name for c in turn.tool_calls]
        state.tools_used += names
        if on_turn is not None:
            await on_turn(names)

    async def on_empty_turn(turn: Any) -> bool:
        # a plain text turn IS the answer (conversational, unlike the
        # investigation engine which demands a structured conclude)
        if state.final_answer is None:
            state.final_answer = turn.text
        return True

    async def cancelled() -> bool:
        return should_cancel is not None and await should_cancel()

    # a bound channel character gives the assistant its voice (framing only —
    # the grounding/citation discipline in the tools is unchanged)
    from connect.content.channel import persona_lines
    system = WS_AGENT_SYSTEM + ("\n\n" + persona_lines(character)
                                if character is not None else "")
    turns = await run_metered_tool_loop(
        llm, system=system, tools=WS_TOOL_DEFS, executor=executor,
        messages=msgs, tier=ModelTier.BALANCED, max_tokens=2048,
        max_iters=max_iters, terminal_tool="final_answer",
        should_force=should_force, after_turn=after_turn,
        on_empty_turn=on_empty_turn, is_concluded=lambda: state.concluded,
        should_cancel=cancelled)

    return WorkspaceAgentResult(
        final_answer=state.final_answer or "",
        messages=msgs, tools_used=state.tools_used, finding=state.finding,
        drafts=state.drafts, turns_completed=turns,
        spent_usd=round(budget.spent_usd, 6),
        budget_remaining_usd=round(budget.remaining_usd, 6))
