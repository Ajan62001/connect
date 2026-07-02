"""Offline regression for the analysis eval harness — MockProvider only, no
network. Covers the pure metrics, the EvalContext seam, and each of the four
evaluators end-to-end against scripted model outputs, so the harness wiring and
the deterministic weighting/verdict math stay correct in CI without spending a
token. The live quality numbers come from `python -m evals` (gated on a key).
"""

from __future__ import annotations

import pytest
from mock_llm import MockProvider

from connect.analysis.schema import (
    ClaimReasoning,
    DecomposedClaim,
    NormalizedInput,
    StanceJudgment,
)
from connect.llm.tiers import ModelTier
from evals import harness, metrics
from evals.cases import (
    MenuItem,
    NormalizeCase,
    ReasoningCase,
    StanceCase,
    VerdictCase,
    VerdictDoc,
)
from evals.context import EvalContext
from evals.judge import NormalizeJudgment, ReasoningJudgment


# -- metrics (pure) -----------------------------------------------------------------


def test_score_classification_perfect():
    pairs = [("supports", "supports"), ("refutes", "refutes")]
    rep = metrics.score_classification(pairs, harness.STANCE_LABELS)
    assert rep.accuracy == 1.0
    assert rep.macro_f1 == 1.0


def test_score_classification_none_prediction_counts_wrong():
    pairs = [("supports", "supports"), ("refutes", None)]
    rep = metrics.score_classification(pairs, harness.STANCE_LABELS)
    assert rep.accuracy == 0.5
    # the dropped prediction lands in the ∅ column for gold 'refutes'
    assert rep.confusion["refutes"][metrics.NONE_LABEL] == 1


def test_score_classification_confusion_offdiagonal():
    pairs = [("supports", "refutes"), ("supports", "supports")]
    rep = metrics.score_classification(pairs, harness.STANCE_LABELS)
    assert rep.confusion["supports"]["refutes"] == 1
    assert rep.confusion["supports"]["supports"] == 1


# -- EvalContext seam ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_context_delegates_to_provider():
    out = NormalizedInput(subject="x", input_kind="other")
    provider = MockProvider(respond_by_schema={NormalizedInput: out})
    ctx = EvalContext(provider)
    completion = await ctx.call_structured(
        system="s", user="u", schema=NormalizedInput, tier=ModelTier.BALANCED,
        max_tokens=100, est_in=1, est_out=1)
    assert completion.output is out
    assert provider.calls[0]["schema"] is NormalizedInput


# -- stance evaluator ---------------------------------------------------------------


def _stance_responder(user_text: str) -> StanceJudgment:
    # branch on a marker present in the document body of _stance_user(...)
    if "ALPHA" in user_text:
        return StanceJudgment(stance="supports",
                              quoted_span="ALPHA supports the claim.",
                              relevance=0.9)
    if "BETA" in user_text:
        return StanceJudgment(stance="refutes",
                              quoted_span="BETA refutes the claim.",
                              relevance=0.9)
    # GAMMA: right stance but an ungrounded span (not in the doc)
    return StanceJudgment(stance="supports",
                          quoted_span="this text is not in the document",
                          relevance=0.5)


@pytest.mark.asyncio
async def test_run_stance_accuracy_and_grounding():
    dataset = [
        StanceCase(claim="c", doc_title="a",
                   doc_text="ALPHA supports the claim.", gold="supports"),
        StanceCase(claim="c", doc_title="b",
                   doc_text="BETA refutes the claim.", gold="refutes"),
        StanceCase(claim="c", doc_title="g",
                   doc_text="GAMMA says something.", gold="supports"),
    ]
    provider = MockProvider(
        respond_by_schema={StanceJudgment: _stance_responder})
    rep = await harness.run_stance(provider, dataset)
    # all three stances are classified correctly...
    assert rep.classification.accuracy == 1.0
    # ...but GAMMA's quoted_span is not verbatim, so grounding < 1
    assert rep.summary["grounded_rate"] == pytest.approx(2 / 3)


# -- verdict evaluator --------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_verdict_supported_from_two_sources():
    def responder(user_text: str) -> StanceJudgment:
        if "Source one" in user_text:
            return StanceJudgment(stance="supports",
                                  quoted_span="Source one supports Q.",
                                  relevance=0.9)
        return StanceJudgment(stance="supports",
                              quoted_span="Source two supports Q.",
                              relevance=0.9)

    dataset = [VerdictCase(
        claim="Q",
        docs=[
            VerdictDoc(title="1", text="Source one supports Q.",
                       credibility_tier=1, domain="a.com"),
            VerdictDoc(title="2", text="Source two supports Q.",
                       credibility_tier=2, domain="b.com"),
        ],
        gold="supported")]
    provider = MockProvider(respond_by_schema={StanceJudgment: responder})
    rep = await harness.run_verdict(provider, dataset)
    assert rep.classification.accuracy == 1.0


@pytest.mark.asyncio
async def test_run_verdict_unverified_when_all_dropped():
    # every judgment is ungrounded -> stance_one discards all -> empty ->
    # compute_verdict returns 'unverified'
    def responder(_user_text: str) -> StanceJudgment:
        return StanceJudgment(stance="supports",
                              quoted_span="not in any document",
                              relevance=0.9)

    dataset = [VerdictCase(
        claim="Q",
        docs=[VerdictDoc(title="1", text="Some text.", credibility_tier=1,
                         domain="a.com")],
        gold="unverified")]
    provider = MockProvider(respond_by_schema={StanceJudgment: responder})
    rep = await harness.run_verdict(provider, dataset)
    assert rep.classification.accuracy == 1.0


# -- normalize evaluator (stage + judge) --------------------------------------------


@pytest.mark.asyncio
async def test_run_normalize_pass():
    normalized = NormalizedInput(
        subject="a scheme", input_kind="policy",
        claims=[
            DecomposedClaim(id="C1", text="The scheme sanctioned houses.",
                            kind="factual", checkable=True),
            DecomposedClaim(id="C2", text="The deadline was extended.",
                            kind="factual", checkable=True),
        ])
    verdict = NormalizeJudgment(atomicity=1.0, kind_accuracy=1.0,
                                checkable_honesty=1.0, overall_pass=True)
    provider = MockProvider(respond_by_schema={
        NormalizedInput: normalized, NormalizeJudgment: verdict})
    dataset = [NormalizeCase(input_text="...", expected_kind="policy",
                             min_claims=2, max_claims=4)]
    rep = await harness.run_normalize(provider, dataset)
    assert rep.summary["pass_rate"] == 1.0
    assert rep.summary["mean_atomicity"] == 1.0


@pytest.mark.asyncio
async def test_run_normalize_fails_bounds():
    # judge passes, but the claim count violates the case's min_claims
    normalized = NormalizedInput(subject="x", input_kind="other", claims=[])
    verdict = NormalizeJudgment(atomicity=1.0, kind_accuracy=1.0,
                                checkable_honesty=1.0, overall_pass=True)
    provider = MockProvider(respond_by_schema={
        NormalizedInput: normalized, NormalizeJudgment: verdict})
    dataset = [NormalizeCase(input_text="...", min_claims=2, max_claims=4)]
    rep = await harness.run_normalize(provider, dataset)
    assert rep.summary["pass_rate"] == 0.0
    assert rep.summary["bounds_ok_rate"] == 0.0


# -- reasoning evaluator (stage + judge) --------------------------------------------


@pytest.mark.asyncio
async def test_run_reasoning_faithful():
    reasoning = ClaimReasoning(reasoning="The evidence [E1] supports it.",
                               cited_evidence_ids=["E1"])
    verdict = ReasoningJudgment(faithful=True, uses_outside_knowledge=False,
                                quality=0.9)
    provider = MockProvider(respond_by_schema={
        ClaimReasoning: reasoning, ReasoningJudgment: verdict})
    dataset = [ReasoningCase(
        claim="c", verdict="supported",
        menu=[MenuItem(quote="A supporting quote.", source_name="Src",
                       credibility_tier=1, stance="supports")])]
    rep = await harness.run_reasoning(provider, dataset)
    assert rep.summary["faithful_rate"] == 1.0
    assert rep.summary["outside_knowledge_rate"] == 0.0
    assert rep.summary["pass_rate"] == 1.0
