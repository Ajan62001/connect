"""Typed why-question generation — ONE BALANCED structured call after the
scope stage (design §3.1). Persisted question rows (status 'open') are the
loop's work queue; a generation failure degrades to an empty queue (the
agent raises its own questions mid-loop) and never fails the run.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from connect.analysis.budget import AnalysisBudget
from connect.investigation.prompts import QUESTION_GEN_SYSTEM
from connect.investigation.schema import (
    GeneratedQuestions,
    ScopePack,
)
from connect.investigation.scoping import render_scope_pack
from connect.llm import spend
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import Governor
from connect.llm.tiers import ModelTier
from connect.storage.db import utc_now

log = logging.getLogger(__name__)

PURPOSE_INVESTIGATION = "investigation"
EST_QGEN_IN, EST_QGEN_OUT = 3500, 700
QGEN_MAX_TOKENS = 1500


async def generate_questions(
        conn: sqlite3.Connection, provider: LLMProvider, *,
        dossier_id: int, pack: ScopePack, governor: Governor,
        budget: AnalysisBudget,
        emit: Callable[[str, dict[str, Any]], Any]) -> list[int]:
    """Generate <=10 typed questions bound to pack items; returns the new
    question row ids (empty on any failure — never fatal)."""
    model = provider.model_for(ModelTier.BALANCED)
    projected = spend.cost_usd(model, input_tokens=EST_QGEN_IN,
                               output_tokens=EST_QGEN_OUT)
    try:
        governor.check(projected)
        budget.check(projected)
    except RuntimeError as e:
        log.warning("question generation skipped (budget): %s", e)
        return []
    user = (render_scope_pack(pack)
            + "\nGenerate the opening question list for this"
              " investigation.")
    try:
        completion = await provider.complete_structured(
            system=QUESTION_GEN_SYSTEM,
            messages=[{"role": "user", "content": user}],
            schema=GeneratedQuestions, tier=ModelTier.BALANCED,
            max_tokens=QGEN_MAX_TOKENS)
    except LLMError as e:
        log.warning("question generation failed: %s", e)
        return []
    spend.record_call(conn, purpose=PURPOSE_INVESTIGATION,
                      model=completion.model, usage=completion.usage)
    budget.add(spend.cost_usd(
        completion.model,
        input_tokens=completion.usage.input_tokens,
        output_tokens=completion.usage.output_tokens,
        cache_read_tokens=completion.usage.cache_read_tokens))

    ids: list[int] = []
    now = utc_now()
    for q in completion.output.questions:
        text = q.text.strip()
        if not text:
            continue
        about_type, about_id = q.about_type, q.about_id
        if about_type is None or about_id is None:
            about_type = about_id = None
        with conn:
            cur = conn.execute(
                "INSERT INTO question (dossier_id, qtype, text, about_type,"
                " about_id, status, priority, created_at)"
                " VALUES (?,?,?,?,?, 'open', ?, ?)",
                (dossier_id, q.qtype, text, about_type, about_id,
                 q.priority, now))
        question_id = int(cur.lastrowid)  # type: ignore[arg-type]
        ids.append(question_id)
        emit("question_raised", {"question_id": question_id,
                                 "qtype": q.qtype, "text": text})
    return ids


def render_questions(conn: sqlite3.Connection, dossier_id: int) -> str:
    """The open-question block appended to the loop's first user message."""
    rows = conn.execute(
        "SELECT id, qtype, text, status, priority FROM question"
        " WHERE dossier_id = ? ORDER BY priority DESC, id",
        (dossier_id,)).fetchall()
    if not rows:
        return ("\nOPEN QUESTIONS: none generated — raise your own with"
                " raise_question as you investigate.\n")
    lines = ["", "OPEN QUESTIONS (your work queue; resolve via"
                 " record_finding question_id + conclude):"]
    for r in rows:
        lines.append(f"- question #{r['id']} [{r['qtype']},"
                     f" priority {r['priority']:.1f}] {r['text']}")
    lines.append("")
    return "\n".join(lines)
