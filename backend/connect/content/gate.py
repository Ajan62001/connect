"""Publish-time editorial verification gate (S2).

Upgrades the structural "cannot cite off-menu" guarantee into a semantic "the
published sentence is actually supported by its evidence" check, at the moment
text becomes public. Two passes:

1. DETERMINISTIC numeric/date coverage. Every number, percentage, or year in a
   published factual sentence must appear in the verbatim source quotes the item
   is grounded in. A miss is the highest-signal journalism failure (a fabricated
   or drifted figure) — but numbers are often re-expressed ("₹2 lakh crore" vs
   "2,00,000 crore"), so a deterministic miss only marks a sentence SUSPECT.

2. LLM ENTAILMENT adjudication of the suspects (FAST tier, bounded count). Only a
   model verdict of not_entailed/partial actually flags the sentence, which
   keeps false positives low. This pass FAILS OPEN: any provider/budget error
   records ``error`` and stops checking WITHOUT flagging, so an LLM outage can
   never halt publishing.

Plus a corpus cross-check: if the item's cited documents carry a claim the
verdict graph marks 'refuted'/'mixed' or that sits in an OPEN contradiction, the
item is surfaced as building on contested evidence.

Launches WARN-ONLY: a 'flagged' verdict is a soft, owner-visible hold; the
``integrity_gate_enforcing`` setting turns it into a hard publish-block once the
entailment precision is validated by S5's live evals.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field, field_validator

from connect.analysis import entailment
from connect.analysis.budget import AnalysisBudget, AnalysisBudgetExceeded
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.spend import BudgetExceeded

# cost/provider failures the gate tolerates by failing OPEN (never blocks)
_SOFT_ERRORS = (LLMError, BudgetExceeded, AnalysisBudgetExceeded)

# how many suspect sentences we will spend a FAST entailment call on per item
MAX_ENTAILMENT_CALLS = 6

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MARKER = re.compile(r"\[\[E\d+\]\]")
_NOISE = re.compile(r"(https?://\S+|[#@]\w+)")
# a number/percentage/year token (commas/decimals kept for now)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


class GateFinding(BaseModel):
    # bounds TRUNCATE rather than reject: statements/claims come from unbounded
    # generated text and DB rows, and a ValidationError here would crash the
    # gate instead of failing open (its core contract).
    statement: str
    reason: Literal["numeric_unsupported", "not_entailed", "partial",
                    "ungrounded"]
    detail: str = ""

    @field_validator("statement", mode="before")
    @classmethod
    def _clip_statement(cls, v: Any) -> Any:
        return v[:600] if isinstance(v, str) else v

    @field_validator("detail", mode="before")
    @classmethod
    def _clip_detail(cls, v: Any) -> Any:
        return v[:400] if isinstance(v, str) else v


class ContestedClaim(BaseModel):
    claim_id: int
    text: str
    verdict: str

    @field_validator("text", mode="before")
    @classmethod
    def _clip_text(cls, v: Any) -> Any:
        return v[:600] if isinstance(v, str) else v


class GateReport(BaseModel):
    verdict: Literal["pass", "flagged", "error"] = "pass"
    checked: int = 0
    flagged: list[GateFinding] = Field(default_factory=list)
    contested: list[ContestedClaim] = Field(default_factory=list)
    llm_calls: int = 0
    enforcing: bool = False
    error: str | None = None

    def blocks(self) -> bool:
        """Would this report block publishing? Only in enforcing mode, and only
        on a definite 'flagged' verdict — an 'error' (fail-open) never blocks."""
        return self.enforcing and self.verdict == "flagged"


# keys in a stored content payload that are NOT verifiable prose
_SKIP_KEYS = {"hashtags", "image_query", "image_queries"}


def texts_from_content(content: Any) -> list[str]:
    """Collect verifiable prose strings from a stored content_item payload,
    format-agnostically (so the async re-verify job needn't reconstruct each
    format's pydantic model). Skips hashtag/image-query fields."""
    out: list[str] = []

    def walk(node: Any, key: str | None = None) -> None:
        if key in _SKIP_KEYS:
            return
        if isinstance(node, str):
            if node.strip():
                out.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key)

    walk(content)
    return out


# -- text mechanics ----------------------------------------------------------

def _scrub(s: str) -> str:
    return _NOISE.sub(" ", _MARKER.sub(" ", s or "")).strip()


def _sentences(texts: list[str]) -> list[str]:
    out: list[str] = []
    for t in texts:
        for piece in _SENT_SPLIT.split(t or ""):
            c = _scrub(piece)
            if c:
                out.append(c)
    return out


def _is_factual(s: str) -> bool:
    """A sentence worth checking: it carries a number/year, or it is a normal
    declarative sentence (not a one-word hook or a bare hashtag line)."""
    if _NUM.search(s):
        return True
    return len(s.split()) >= 7


def _numbers(s: str) -> list[str]:
    return [m.group(0) for m in _NUM.finditer(s)]


def _num_key(tok: str) -> str:
    return tok.replace(",", "")


# -- the gate ----------------------------------------------------------------

async def _contested(conn: psycopg.AsyncConnection,
                     document_ids: list[int]) -> list[ContestedClaim]:
    ids = [d for d in {int(x) for x in document_ids if x}]
    if not ids:
        return []
    cur = await conn.execute(
        "SELECT DISTINCT c.id, c.text, c.verdict"
        " FROM claim_sighting cs JOIN claim c ON c.id = cs.claim_id"
        " LEFT JOIN contradiction ct"
        "   ON ct.claim_id = c.id AND ct.status = 'open'"
        " WHERE cs.document_id = ANY(%s)"
        "   AND (c.verdict IN ('refuted', 'mixed') OR ct.id IS NOT NULL)"
        " LIMIT 20",
        (ids,))
    return [ContestedClaim(claim_id=r["id"], text=r["text"] or "",
                           verdict=r["verdict"]) for r in await cur.fetchall()]


async def assess(conn: psycopg.AsyncConnection, provider: LLMProvider, *,
                 content_texts: list[str], source_quotes: list[str],
                 document_ids: list[int], budget: AnalysisBudget, governor: Any,
                 viewer: int | None, enforcing: bool = False) -> GateReport:
    """Run the gate over already-resolved content. ``source_quotes`` is the union
    of verbatim quotes the item is grounded in; ``document_ids`` the cited docs
    (for the contested cross-check)."""
    report = GateReport(enforcing=enforcing)
    quotes = [q for q in source_quotes if q and q.strip()]
    evidence_text = "\n".join(f"- {q}" for q in quotes)
    # the set of figures the evidence actually states (exact token match — a
    # substring test would wrongly accept '2' because it occurs inside '200000')
    evidence_nums = {_num_key(n) for n in _numbers(evidence_text)}

    suspects: list[str] = []
    for s in _sentences(content_texts):
        if not _is_factual(s):
            continue
        report.checked += 1
        if not quotes:
            report.flagged.append(GateFinding(
                statement=s, reason="ungrounded",
                detail="the item has no grounded source quotes"))
            continue
        missing = [n for n in _numbers(s) if _num_key(n) not in evidence_nums]
        if missing:
            suspects.append(s)

    for s in suspects[:MAX_ENTAILMENT_CALLS]:
        try:
            j = await entailment.score(
                conn, provider, evidence=evidence_text, statement=s,
                budget=budget, governor=governor, viewer=viewer)
        except _SOFT_ERRORS as e:
            # fail OPEN: a provider/budget failure records the error and stops
            # checking WITHOUT flagging, so an outage can never block publishing.
            report.error = f"{type(e).__name__}: {e}"
            break
        report.llm_calls += 1
        if j.label in ("not_entailed", "partial"):
            report.flagged.append(GateFinding(
                statement=s,
                reason=("not_entailed" if j.label == "not_entailed"
                        else "partial"),
                detail=j.rationale))

    report.contested = await _contested(conn, document_ids)
    if report.flagged:
        report.verdict = "flagged"
    elif report.error:
        report.verdict = "error"
    return report
