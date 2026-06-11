"""The synthesize stage — code-rendered sections + ONE DEEP structured
call over the CLOSED findings menu (design §5).

Synthesis grounding: the model sees ONLY the findings menu (f<id> ->
kind/text/quote/document_id/speculation), the timeline and the question
list. Post-hoc, in code: every [[f#]] marker must resolve to a menu id
(unknown -> one regeneration, then strip + flag in grounding_report);
finding_ids are filtered to real ids; chains citing speculative findings
inherit speculation; watch_next items must anchor to a question or a
calendar id. Re-runnable from rows alone.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

import psycopg

from connect.analysis.budget import AnalysisBudget, AnalysisBudgetExceeded
from connect.investigation.prompts import SYNTHESIS_SYSTEM
from connect.investigation.schema import ScopePack, SynthesisOutput
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier
from connect.storage.edges import CAUSAL_RELATIONS

log = logging.getLogger(__name__)

PURPOSE_INVESTIGATION_SYNTHESIS = "investigation_synthesis"
EST_SYN_IN, EST_SYN_OUT = 6000, 1800
SYN_MAX_TOKENS = 3000

CITATION_RE = re.compile(r"\[\[f(\d+)\]\]")

SectionWriter = Callable[..., Any]   # async (dossier_id, stage, content, status=)


# -- findings menu -------------------------------------------------------------------


async def findings_menu(conn: psycopg.AsyncConnection,
                        dossier_id: int) -> list[dict[str, Any]]:
    """The closed evidence menu: one row per finding with its first quote."""
    cur = await conn.execute(
        "SELECT f.id, f.kind, f.text, f.speculation, f.confidence,"
        " f.question_id, f.edge_id,"
        " (SELECT quote FROM finding_evidence fe WHERE fe.finding_id = f.id"
        "  ORDER BY fe.id LIMIT 1) AS quote,"
        " (SELECT document_id FROM finding_evidence fe"
        "  WHERE fe.finding_id = f.id ORDER BY fe.id LIMIT 1)"
        "  AS document_id"
        " FROM finding f WHERE f.dossier_id = %s ORDER BY f.id",
        (dossier_id,))
    rows = await cur.fetchall()
    return [{"finding_id": r["id"], "kind": r["kind"], "text": r["text"],
             "speculation": bool(r["speculation"]),
             "confidence": r["confidence"], "quote": r["quote"],
             "document_id": r["document_id"],
             "question_id": r["question_id"], "edge_id": r["edge_id"]}
            for r in rows]


def render_menu(menu: list[dict[str, Any]]) -> str:
    lines = ["FINDINGS MENU (cite as [[f<id>]]; this is everything you"
             " know):"]
    for m in menu:
        spec = " SPECULATIVE" if m["speculation"] else ""
        quote = f' quote: "{m["quote"]}"' if m["quote"] else ""
        doc = (f" (document #{m['document_id']})"
               if m["document_id"] else "")
        lines.append(f"[f{m['finding_id']}] [{m['kind']}{spec}]"
                     f" {m['text']}{quote}{doc}")
    if len(lines) == 1:
        lines.append("(no findings were recorded)")
    return "\n".join(lines)


def check_citations(text: str, known_ids: set[int]) -> list[str]:
    """Unknown [[f#]] markers in the text (order-preserving, deduped)."""
    bad: list[str] = []
    for match in CITATION_RE.finditer(text or ""):
        if int(match.group(1)) not in known_ids:
            marker = match.group(0)
            if marker not in bad:
                bad.append(marker)
    return bad


def strip_markers(text: str, markers: list[str]) -> str:
    out = text or ""
    for marker in markers:
        out = out.replace(marker, "")
    return out


# -- code sections ----------------------------------------------------------------------


async def timeline_section(conn: psycopg.AsyncConnection,
                           pack: ScopePack) -> dict[str, Any]:
    """Timeline items with causal chips read FRESH from the edge table so
    edges written during the loop appear immediately."""
    items: list[dict[str, Any]] = []
    for item in pack.timeline:
        causal: list[dict[str, Any]] = []
        if item.event_id is not None:
            cur = await conn.execute(
                "SELECT id, src_type, src_id, dst_type, dst_id, relation,"
                " properties FROM edge WHERE status = 'active'"
                " AND relation = ANY(%s)"
                " AND ((src_type = 'event' AND src_id = %s)"
                "  OR (dst_type = 'event' AND dst_id = %s))",
                (list(CAUSAL_RELATIONS), item.event_id, item.event_id))
            rows = await cur.fetchall()
            for row in rows:
                outgoing = (row["src_type"] == "event"
                            and row["src_id"] == item.event_id)
                other_type = (row["dst_type"] if outgoing
                              else row["src_type"])
                other_id = row["dst_id"] if outgoing else row["src_id"]
                properties = (row["properties"]
                              if isinstance(row["properties"], dict) else {})
                speculation = bool(properties.get("speculation"))
                title_row = None
                if other_type == "event":
                    tcur = await conn.execute(
                        "SELECT title AS t FROM event WHERE id = %s",
                        (other_id,))
                    title_row = await tcur.fetchone()
                elif other_type == "entity":
                    tcur = await conn.execute(
                        "SELECT name AS t FROM entity WHERE id = %s",
                        (other_id,))
                    title_row = await tcur.fetchone()
                causal.append({
                    "edge_id": row["id"], "relation": row["relation"],
                    "direction": "out" if outgoing else "in",
                    "other_type": other_type, "other_id": other_id,
                    "other_title": title_row["t"] if title_row else None,
                    "speculation": speculation})
        items.append({
            "event_id": item.event_id, "document_id": item.document_id,
            "date": item.date, "title": item.title,
            "event_type": item.event_type, "doc_count": item.doc_count,
            "causal": causal})
    return {"items": items}


async def alternatives_section(conn: psycopg.AsyncConnection,
                               dossier_id: int) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT id, text FROM finding WHERE dossier_id = %s"
        " AND kind = 'alternative' ORDER BY id", (dossier_id,))
    rows = await cur.fetchall()
    items = [{"title": (r["text"][:80] + "…" if len(r["text"]) > 80
                        else r["text"]),
              "description": r["text"], "finding_ids": [r["id"]],
              "by_whom_entity_id": None} for r in rows]
    cur = await conn.execute(
        "SELECT id FROM question WHERE dossier_id = %s"
        " AND qtype = 'why_not_alternative' AND status IN ('open',"
        " 'partial') ORDER BY id", (dossier_id,))
    unanswered = [r["id"] for r in await cur.fetchall()]
    return {"items": items, "unanswered_question_ids": unanswered}


async def open_questions_section(conn: psycopg.AsyncConnection,
                                 dossier_id: int) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT id, qtype, text, status, priority, spawned_dossier_id"
        " FROM question WHERE dossier_id = %s"
        " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'partial' THEN 1"
        " ELSE 2 END, priority DESC, id", (dossier_id,))
    rows = await cur.fetchall()
    return {"items": [
        {"question_id": r["id"], "qtype": r["qtype"], "text": r["text"],
         "status": r["status"], "priority": r["priority"],
         "spawned_dossier_id": r["spawned_dossier_id"]} for r in rows]}


# -- the DEEP call + post-hoc grounding ----------------------------------------------------


async def _synthesis_user(conn: psycopg.AsyncConnection, dossier_id: int,
                          pack: ScopePack,
                          menu: list[dict[str, Any]]) -> str:
    parts = [f"INVESTIGATION: {pack.input_text}", "", render_menu(menu), ""]
    if pack.timeline:
        parts.append("TIMELINE:")
        for item in pack.timeline:
            ref = (f"event #{item.event_id}" if item.event_id
                   else f"document #{item.document_id}")
            parts.append(f"- {item.date or 'undated'} | {ref}"
                         f" | {item.title}")
        parts.append("")
    if pack.calendar:
        parts.append("CALENDAR ITEMS (anchor watch_next to these ids or"
                     " to question ids):")
        parts.extend(
            f"- calendar #{c.calendar_event_id} {c.occurs_on} {c.label}"
            for c in pack.calendar)
        parts.append("")
    cur = await conn.execute(
        "SELECT id, qtype, text, status FROM question"
        " WHERE dossier_id = %s ORDER BY id", (dossier_id,))
    rows = await cur.fetchall()
    if rows:
        parts.append("QUESTIONS:")
        parts.extend(f"- question #{r['id']} [{r['qtype']}, {r['status']}]"
                     f" {r['text']}" for r in rows)
        parts.append("")
    parts.append("Write the synthesis sections now.")
    return "\n".join(parts)


async def _resolve_actor_entity(conn: psycopg.AsyncConnection,
                                entity_id: int | None,
                                name: str) -> int | None:
    """Untrusted-id repair for the actors section. The DEEP call sees only
    the finding/question menus — never the entity table — so its actor
    entity ids are routinely fabricated. Resolve by name (exact name or
    alias, case-insensitive, with a parenthetical-acronym strip: 'Reserve
    Bank of India (RBI)' -> 'reserve bank of india'); keep the supplied id
    only when it denotes an entity whose name is consistent with the
    actor's; otherwise None (the UI renders an unlinked actor card)."""
    norm = (name or "").strip().lower()
    base = norm.split("(", 1)[0].strip()
    for candidate in dict.fromkeys((norm, base)):   # ordered, de-duped
        if not candidate:
            continue
        cur = await conn.execute(
            "SELECT id FROM entity WHERE lower(name) = %s",
            (candidate,))
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute(
                "SELECT e.id FROM entity e,"
                " jsonb_array_elements_text(e.aliases) a"
                " WHERE lower(a.value) = %s LIMIT 1", (candidate,))
            row = await cur.fetchone()
        if row is not None:
            return int(row["id"])
    if entity_id is not None:
        cur = await conn.execute("SELECT name FROM entity WHERE id = %s",
                                 (entity_id,))
        row = await cur.fetchone()
        if row is not None:
            existing = row["name"].strip().lower()
            if existing and (existing in norm
                             or (base and base in existing)):
                return int(entity_id)
    return None


async def _ground_output(conn: psycopg.AsyncConnection, dossier_id: int,
                         pack: ScopePack, output: SynthesisOutput,
                         menu: list[dict[str, Any]],
                         stripped: list[str], regenerated: bool,
                         ) -> tuple[dict[str, Any], dict[str, Any],
                                    dict[str, Any]]:
    """(causal_narrative, actors, watch_next) section contents after the
    mechanical grounding pass."""
    known = {m["finding_id"] for m in menu}
    speculative = {m["finding_id"] for m in menu if m["speculation"]}

    narrative = strip_markers(output.narrative_md, stripped)

    chains: list[dict[str, Any]] = []
    for chain in output.chains:
        steps: list[dict[str, Any]] = []
        chain_spec = chain.speculation
        for step in chain.steps:
            finding_id = (step.finding_id
                          if step.finding_id in known else None)
            spec = step.speculation or (finding_id in speculative)
            if spec:
                chain_spec = True
            relation = step.relation_to_next
            if relation is not None and relation not in CAUSAL_RELATIONS:
                relation = None
            steps.append({
                "node_type": step.node_type, "node_id": step.node_id,
                "title": step.title, "relation_to_next": relation,
                "speculation": spec, "finding_id": finding_id})
        chains.append({"steps": steps, "speculation": chain_spec})

    actors: list[dict[str, Any]] = []
    for actor in output.actors:
        finding_ids = [f for f in actor.finding_ids if f in known]
        motive_md = strip_markers(actor.motive_md, stripped)
        actors.append({
            "entity_id": await _resolve_actor_entity(
                conn, actor.entity_id, actor.name),
            "name": actor.name,
            "role": actor.role, "motive_md": motive_md,
            "finding_ids": finding_ids,
            "speculation": actor.speculation
            or any(f in speculative for f in finding_ids)})

    qcur = await conn.execute(
        "SELECT id FROM question WHERE dossier_id = %s", (dossier_id,))
    question_ids = {r["id"] for r in await qcur.fetchall()}
    calendar_ids = {c.calendar_event_id for c in pack.calendar}
    watch_items: list[dict[str, Any]] = []
    dropped_watch = 0
    for item in output.watch_next:
        question_id = (item.question_id
                       if item.question_id in question_ids else None)
        calendar_event_id = (item.calendar_event_id
                             if item.calendar_event_id in calendar_ids
                             else None)
        if question_id is None and calendar_event_id is None:
            dropped_watch += 1   # must anchor to a question or calendar id
            continue
        watch_items.append({
            "text": item.text, "question_id": question_id,
            "calendar_event_id": calendar_event_id,
            "watch_suggestion": item.watch_suggestion})

    grounding_report = {
        "stripped_citations": stripped,
        "regenerated": regenerated,
        "dropped_watch_items": dropped_watch,
    }
    causal_narrative = {"narrative_md": narrative, "chains": chains,
                        "grounding_report": grounding_report}
    return causal_narrative, {"actors": actors}, {"items": watch_items}


async def run(conn: psycopg.AsyncConnection, provider: LLMProvider, *,
              dossier_id: int, pack: ScopePack, budget: AnalysisBudget,
              governor: Governor, emit: Callable[..., Any],
              tier_pref: str = "deep", reserve_usd: float = 0.20,
              section_writer: SectionWriter) -> str:
    """Persist all six output sections; returns the stage summary."""
    notes: list[str] = []

    # code sections first — they exist even if the DEEP call fails
    for stage, content in (
            ("timeline", await timeline_section(conn, pack)),
            ("alternatives", await alternatives_section(conn, dossier_id)),
            ("open_questions",
             await open_questions_section(conn, dossier_id))):
        await section_writer(dossier_id, stage, content)
        await emit("section_completed", {"section": stage})

    menu = await findings_menu(conn, dossier_id)
    known = {m["finding_id"] for m in menu}

    # tier choice: DEEP unless the run already ate into the synthesis
    # reserve and the DEEP projection no longer fits what is left
    tier = (ModelTier.DEEP if tier_pref == "deep" else ModelTier.BALANCED)
    if tier is ModelTier.DEEP:
        deep_cost = spend.cost_usd(provider.model_for(ModelTier.DEEP),
                                   input_tokens=EST_SYN_IN,
                                   output_tokens=EST_SYN_OUT)
        if deep_cost > budget.remaining_usd:
            tier = ModelTier.BALANCED
            notes.append(
                "synthesis degraded to BALANCED (over the"
                f" ${reserve_usd:.2f} reserve)")
    projected = spend.cost_usd(provider.model_for(tier),
                               input_tokens=EST_SYN_IN,
                               output_tokens=EST_SYN_OUT)
    user = await _synthesis_user(conn, dossier_id, pack, menu)

    output: SynthesisOutput | None = None
    stripped: list[str] = []
    regenerated = False
    try:
        await governor.check(projected)
        output = await _call(conn, provider, budget, user, tier)
        bad = check_citations(output.narrative_md, known)
        for actor in output.actors:
            bad.extend(m for m in check_citations(actor.motive_md, known)
                       if m not in bad)
        if bad:
            regenerated = True
            retry_user = (
                f"{user}\n\nPREVIOUS ATTEMPT REJECTED: you cited unknown"
                f" finding ids: {', '.join(bad)}. Cite ONLY ids on the"
                " findings menu.")
            try:
                await governor.check(projected)
                retry = await _call(conn, provider, budget, retry_user,
                                    tier)
                retry_bad = check_citations(retry.narrative_md, known)
                for actor in retry.actors:
                    retry_bad.extend(
                        m for m in check_citations(actor.motive_md, known)
                        if m not in retry_bad)
                output, bad = retry, retry_bad
            except (BudgetExceeded, AnalysisBudgetExceeded, LLMError) as e:
                log.warning("synthesis regeneration unavailable: %s", e)
            stripped = bad   # whatever remains is stripped + flagged
            if stripped:
                notes.append(f"stripped {len(stripped)} unknown"
                             " citation(s)")
    except (BudgetExceeded, AnalysisBudgetExceeded) as e:
        log.warning("synthesis skipped (budget): %s", e)
        for stage in ("causal_narrative", "actors", "watch_next"):
            await section_writer(dossier_id, stage,
                                 {"skipped": f"budget: {e}"},
                                 status="skipped")
        return "synthesis skipped (budget); code sections persisted"

    causal_narrative, actors, watch_next = await _ground_output(
        conn, dossier_id, pack, output, menu, stripped, regenerated)
    for stage, content in (("causal_narrative", causal_narrative),
                           ("actors", actors),
                           ("watch_next", watch_next)):
        await section_writer(dossier_id, stage, content)
        await emit("section_completed", {"section": stage})

    notes.insert(0, f"{len(menu)} findings synthesized on"
                    f" {tier.value} tier")
    return "; ".join(notes)


async def _call(conn: psycopg.AsyncConnection, provider: LLMProvider,
                budget: AnalysisBudget, user: str,
                tier: ModelTier) -> SynthesisOutput:
    completion = await provider.complete_structured(
        system=SYNTHESIS_SYSTEM,
        messages=[{"role": "user", "content": user}],
        schema=SynthesisOutput, tier=tier, max_tokens=SYN_MAX_TOKENS)
    await spend.record_call(conn, purpose=PURPOSE_INVESTIGATION_SYNTHESIS,
                            model=completion.model, usage=completion.usage)
    budget.add(spend.cost_usd(
        completion.model,
        input_tokens=completion.usage.input_tokens,
        output_tokens=completion.usage.output_tokens,
        cache_read_tokens=completion.usage.cache_read_tokens))
    return completion.output
