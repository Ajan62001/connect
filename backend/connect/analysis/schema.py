"""FROZEN stage I/O contracts for the analysis pipeline (engine design §3).

These models double as LLM structured-output schemas (provider-validated)
and as the persisted dossier_section content shapes — the single source of
truth every stage codes against. Size limits are clamping validators, not
schema constraints (over-long output degrades, never fails the stage).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

MAX_CLAIMS = 6
MAX_SEED_QUERIES = 5

ClaimKind = Literal["factual", "causal", "normative", "predictive"]
Stance = Literal["supports", "refutes", "mixed", "unrelated"]
# lowercase to match domain/enums.VERDICTS (claim.verdict CHECK + API)
VerdictLabel = Literal["supported", "refuted", "mixed", "unverified"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# -- stage 0: normalize -------------------------------------------------------------


class DecomposedClaim(_Frozen):
    """One atomic, context-free claim ("C1"); normative/predictive claims
    are honestly flagged checkable=False and excluded from verification."""
    id: str
    text: str
    kind: ClaimKind
    checkable: bool


class NormalizedInput(_Frozen):
    subject: str
    input_kind: Literal[
        "policy", "political_event", "finance_claim", "news_claim", "other"]
    claims: list[DecomposedClaim] = []
    seed_queries: list[str] = []

    @field_validator("claims")
    @classmethod
    def _clamp_claims(cls, v: list[DecomposedClaim]) -> list[DecomposedClaim]:
        return v[:MAX_CLAIMS]

    @field_validator("seed_queries")
    @classmethod
    def _clamp_queries(cls, v: list[str]) -> list[str]:
        return v[:MAX_SEED_QUERIES]


# -- stage 1: verify ----------------------------------------------------------------


class StanceJudgment(_Frozen):
    """One claim x one document. quoted_span MUST verbatim-verify against
    the stored doc text (grounding.span_is_verbatim) or the judgment is
    retried once, then discarded."""
    stance: Stance
    quoted_span: str
    relevance: float
    note: str = ""

    @field_validator("relevance")
    @classmethod
    def _clamp_relevance(cls, v: float) -> float:
        return min(1.0, max(0.0, v))


class ClaimReasoning(_Frozen):
    """BALANCED-written explanation, produced AFTER the verdict enum is
    fixed in code; cited ids are validated against the closed evidence
    menu — off-menu citations reject the output."""
    reasoning: str
    cited_evidence_ids: list[str] = []


class SameProposition(_Frozen):
    """Claim-reconciliation gray-zone tie-break (FAST): are two claim texts
    the same proposition?"""
    same: bool


# -- persisted verify-section shapes (dossier_section content) -----------------------


class EvidenceRef(_Frozen):
    """Audit row inside the verify section content: one weighted stance."""
    evidence_id: int            # evidence table row id
    document_id: int
    stance: Stance
    relevance: float
    weight: float
    quote: str


class ClaimResult(_Frozen):
    """Per-claim verify outcome as persisted (and served by the API)."""
    claim_id: int               # canonical claim row id (reconciled)
    local_id: str               # "C1" from the decomposition
    text: str
    kind: ClaimKind
    checkable: bool
    verdict: VerdictLabel | None = None
    confidence: float | None = None
    score: float | None = None  # raw S, kept for audit
    total_weight: float | None = None
    reasoning: str | None = None
    evidence: list[EvidenceRef] = []
    note: str | None = None     # degrade/abort annotations


class VerdictSummary(_Frozen):
    supported: int = 0
    refuted: int = 0
    mixed: int = 0
    unverified: int = 0
