"""The four analysis evaluators. Each drives the REAL stage code via
EvalContext, grades against the gold dataset (or an LLM judge), and returns an
EvalReport. Async + provider-agnostic: the live CLI passes an AnthropicProvider,
the offline suite passes a MockProvider.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from connect.analysis import grounding
from connect.analysis.context import (
    EST_STANCE_IN,
    EST_STANCE_OUT,
)
from connect.analysis.prompts import STANCE_SYSTEM
from connect.analysis.schema import StanceJudgment
from connect.analysis.stages import normalize, verify
from connect.llm.provider import LLMError, LLMProvider
from connect.llm.tiers import ModelTier

from evals import cases, judge
from evals.context import EvalContext
from evals.metrics import (
    ClassificationReport,
    render_classification,
    score_classification,
)

STANCE_LABELS = ["supports", "refutes", "mixed", "unrelated"]
VERDICT_LABELS = ["supported", "refuted", "mixed", "unverified"]


@dataclass
class EvalReport:
    name: str
    summary: dict[str, Any]
    classification: ClassificationReport | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"\n### {self.name}"]
        if self.classification is not None:
            lines.append(render_classification(self.name, self.classification))
        for k, v in self.summary.items():
            val = f"{v:.3f}" if isinstance(v, float) else str(v)
            lines.append(f"  {k}: {val}")
        return "\n".join(lines)


# -- stance classification ----------------------------------------------------------


async def _classify_stance(ctx: EvalContext, claim: str,
                           doc: dict) -> tuple[str | None, bool]:
    """One stance call via the production prompt + schema. Returns
    (stance, grounded). Unlike verify.stance_one this keeps 'unrelated' and
    does not collapse to None, so the confusion matrix can score it; the
    verbatim-span check is reported separately as the grounding rate."""
    try:
        completion = await ctx.call_structured(
            system=STANCE_SYSTEM, user=verify._stance_user(claim, doc),
            schema=StanceJudgment, tier=ModelTier.FAST,
            max_tokens=verify.STANCE_MAX_OUTPUT_TOKENS,
            est_in=EST_STANCE_IN, est_out=EST_STANCE_OUT)
    except LLMError:
        return None, False
    j = completion.output
    grounded = grounding.span_is_verbatim(j.quoted_span, doc["content_text"])
    return j.stance, grounded


async def run_stance(provider: LLMProvider,
                     dataset: list[cases.StanceCase] | None = None,
                     ) -> EvalReport:
    ctx = EvalContext(provider)
    dataset = dataset if dataset is not None else cases.load_stance()
    results = await asyncio.gather(*(
        _classify_stance(ctx, c.claim,
                         {"id": i, "title": c.doc_title,
                          "content_text": c.doc_text})
        for i, c in enumerate(dataset)))
    pairs = [(c.gold, pred) for c, (pred, _) in zip(dataset, results)]
    # grounding only matters where the model took a non-'unrelated' stance
    relevant = [g for (pred, g), c in zip(results, dataset)
                if pred not in (None, "unrelated")]
    grounded_rate = (sum(relevant) / len(relevant)) if relevant else 1.0
    rep = score_classification(pairs, STANCE_LABELS)
    return EvalReport(
        name="stance classification", classification=rep,
        summary={"grounded_rate": grounded_rate})


# -- end-to-end verdict -------------------------------------------------------------


async def _verdict_for_case(ctx: EvalContext,
                            case: cases.VerdictCase) -> str:
    """Run the production stance + weighting + verdict path over a fixed
    mini-corpus (no DB gather). Mirrors verify._verify_claim's core: drop
    ungrounded/'unrelated' judgments, weigh in (best-tier, id) order, then
    the deterministic verdict."""
    indexed = list(enumerate(case.docs))
    judged: list[tuple[int, cases.VerdictDoc, StanceJudgment]] = []
    for i, d in indexed:
        doc = {"id": i, "title": d.title, "content_text": d.text}
        j = await verify.stance_one(ctx, case.claim, doc)
        if j is not None:
            judged.append((i, d, j))
    # production order: best credibility tier first, then document id
    judged.sort(key=lambda t: (t[1].credibility_tier or 5, t[0]))
    weighed = [verify.WeighedDoc(
        document_id=i, domain=d.domain or f"doc:{i}",
        credibility_tier=d.credibility_tier, stance=j.stance,
        relevance=j.relevance) for i, d, j in judged]
    weights = verify.weigh(weighed)
    stances = [(wd.stance, w) for wd, w in zip(weighed, weights)]
    verdict, _conf, _s, _tot = verify.compute_verdict(stances)
    return verdict


async def run_verdict(provider: LLMProvider,
                      dataset: list[cases.VerdictCase] | None = None,
                      ) -> EvalReport:
    ctx = EvalContext(provider)
    dataset = dataset if dataset is not None else cases.load_verdict()
    preds = await asyncio.gather(*(
        _verdict_for_case(ctx, c) for c in dataset))
    pairs = [(c.gold, p) for c, p in zip(dataset, preds)]
    rep = score_classification(pairs, VERDICT_LABELS)
    return EvalReport(name="end-to-end verdict", classification=rep,
                      summary={})


# -- claim decomposition (judge) ----------------------------------------------------


async def _normalize_one(ctx: EvalContext, provider: LLMProvider,
                         case: cases.NormalizeCase) -> dict[str, Any]:
    normalized = await normalize.run(ctx, case.input_text)
    n = len(normalized.claims)
    bounds_ok = case.min_claims <= n <= case.max_claims
    kind_ok = (case.expected_kind is None
               or normalized.input_kind == case.expected_kind)
    has_uncheckable = any(not c.checkable for c in normalized.claims)
    uncheck_ok = (not case.expect_uncheckable) or has_uncheckable
    j = await judge.judge_normalize(
        provider, input_text=case.input_text, claims=list(normalized.claims))
    return {
        "n_claims": n, "bounds_ok": bounds_ok, "kind_ok": kind_ok,
        "uncheckable_ok": uncheck_ok,
        "atomicity": j.atomicity, "kind_accuracy": j.kind_accuracy,
        "checkable_honesty": j.checkable_honesty,
        "judge_pass": j.overall_pass,
        "pass": bounds_ok and kind_ok and uncheck_ok and j.overall_pass,
    }


async def run_normalize(provider: LLMProvider,
                        dataset: list[cases.NormalizeCase] | None = None,
                        ) -> EvalReport:
    ctx = EvalContext(provider)
    dataset = dataset if dataset is not None else cases.load_normalize()
    rows = await asyncio.gather(*(
        _normalize_one(ctx, provider, c) for c in dataset))
    n = len(rows) or 1
    summary = {
        "n": len(rows),
        "pass_rate": sum(r["pass"] for r in rows) / n,
        "bounds_ok_rate": sum(r["bounds_ok"] for r in rows) / n,
        "kind_ok_rate": sum(r["kind_ok"] for r in rows) / n,
        "mean_atomicity": sum(r["atomicity"] for r in rows) / n,
        "mean_kind_accuracy": sum(r["kind_accuracy"] for r in rows) / n,
        "mean_checkable_honesty":
            sum(r["checkable_honesty"] for r in rows) / n,
    }
    return EvalReport(name="claim decomposition", summary=summary, rows=rows)


# -- reasoning faithfulness (judge) -------------------------------------------------


async def _reasoning_one(ctx: EvalContext, provider: LLMProvider,
                         case: cases.ReasoningCase) -> dict[str, Any]:
    menu = grounding.EvidenceMenu()
    for m in case.menu:
        menu.add(document_id=0, quote=m.quote, source_name=m.source_name,
                 credibility_tier=m.credibility_tier, stance=m.stance)
    reasoning = await verify.write_reasoning(
        ctx, claim_text=case.claim, verdict=case.verdict, menu=menu,
        fallback="(deterministic template fallback)")
    j = await judge.judge_reasoning(
        provider, claim=case.claim, verdict=case.verdict,
        menu_render=menu.render(), explanation=reasoning)
    return {
        "faithful": j.faithful,
        "uses_outside_knowledge": j.uses_outside_knowledge,
        "quality": j.quality,
        "pass": j.faithful and not j.uses_outside_knowledge,
    }


async def run_reasoning(provider: LLMProvider,
                        dataset: list[cases.ReasoningCase] | None = None,
                        ) -> EvalReport:
    ctx = EvalContext(provider)
    dataset = dataset if dataset is not None else cases.load_reasoning()
    rows = await asyncio.gather(*(
        _reasoning_one(ctx, provider, c) for c in dataset))
    n = len(rows) or 1
    summary = {
        "n": len(rows),
        "faithful_rate": sum(r["faithful"] for r in rows) / n,
        "outside_knowledge_rate":
            sum(r["uses_outside_knowledge"] for r in rows) / n,
        "mean_quality": sum(r["quality"] for r in rows) / n,
        "pass_rate": sum(r["pass"] for r in rows) / n,
    }
    return EvalReport(name="reasoning faithfulness", summary=summary,
                      rows=rows)


EVALUATORS = {
    "stance": run_stance,
    "verdict": run_verdict,
    "normalize": run_normalize,
    "reasoning": run_reasoning,
}


async def run_all(provider: LLMProvider,
                  which: list[str] | None = None) -> list[EvalReport]:
    names = which or list(EVALUATORS)
    return [await EVALUATORS[name](provider) for name in names]
