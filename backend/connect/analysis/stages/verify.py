"""Stage 1 — VERIFICATION (engine design §3 stage 2; the trust core).

Per checkable claim:
  2a. evidence gathering — corpus FTS + corpus vectors + SearchClient; up
      to K docs total per claim (K = options.max_evidence_per_claim,
      default settings.analysis_max_evidence); new web hits are fetched
      through the EXISTING ingestion pipeline (immutable snapshots).
  2b. stance classification — FAST, one call per claim x doc, fanned out
      with asyncio.gather; quoted_span MUST verbatim-verify against the
      stored doc text or the judgment gets ONE retry (error shown), then
      is discarded. 'unrelated' judgments are discarded too.
  2c. credibility weighting — PURE CODE: tier weights 1.0/0.8/0.5/0.3,
      unknown 0.2, x relevance; per-domain independence discount: first
      doc per publisher domain counts fully, subsequent ones at 0.3x.
  2d. verdict — S = (Σw_supports − Σw_refutes) / Σw_all (the ±0.5·mixed
      terms cancel); Σw_all < MIN_TOTAL_WEIGHT gates 'unverified'. The
      enum is decided in code; BALANCED writes the reasoning AFTERWARDS,
      constrained to the closed evidence menu (off-menu citations are
      rejected; one retry, then a deterministic template).

Budget policy: before each claim, the projected cost must fit the
per-analysis budget — degrade K to DEGRADED_K first, then abort with a
partial result (remaining checkable claims stay verdict-less, noted).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import psycopg

from connect.analysis import grounding, writeback
from connect.analysis.budget import AnalysisBudgetExceeded
from connect.analysis.context import (
    EST_REASONING_IN,
    EST_REASONING_OUT,
    EST_STANCE_IN,
    EST_STANCE_OUT,
    AnalysisContext,
)
from connect.analysis.prompts import REASONING_SYSTEM, STANCE_SYSTEM
from connect.analysis.schema import (
    ClaimReasoning,
    ClaimResult,
    DecomposedClaim,
    EvidenceRef,
    NormalizedInput,
    StanceJudgment,
)
from connect.knowledge import contradictions
from connect.llm.provider import LLMError
from connect.llm.spend import BudgetExceeded
from connect.llm.tiers import ModelTier
from connect.storage import fts as fts_dao

log = logging.getLogger(__name__)

_Row = Mapping[str, Any]

# -- weighting constants (engine design §3 2c — table-driven in tests) ---------------

TIER_WEIGHTS: dict[int, float] = {1: 1.0, 2: 0.8, 3: 0.5, 4: 0.3}
UNKNOWN_TIER_WEIGHT = 0.2
INDEPENDENCE_DISCOUNT = 0.3   # per-domain: first doc full, rest x0.3

# -- verdict thresholds (engine open question #3: v0.1 picks, tuned later) -----------

MIN_TOTAL_WEIGHT = 0.5        # below this Σw_all the claim stays unverified
SUPPORTED_THRESHOLD = 0.3     # S >= -> supported
REFUTED_THRESHOLD = -0.3      # S <= -> refuted
CONFIDENCE_WEIGHT_SCALE = 3.0  # Σw_all that maps to full weight-confidence

DEGRADED_K = 2                # budget-degraded evidence docs per claim
CORPUS_POOL_LIMIT = 8         # FTS/vec candidates considered before the cap
STANCE_MAX_CONTENT_WORDS = 1200
STANCE_MAX_OUTPUT_TOKENS = 512
REASONING_MAX_OUTPUT_TOKENS = 512


# -- pure math (tested table-driven) ---------------------------------------------------


@dataclass(frozen=True)
class WeighedDoc:
    """One accepted stance judgment with everything weighting needs."""
    document_id: int
    domain: str
    credibility_tier: int | None
    stance: str                 # supports | refutes | mixed
    relevance: float


def tier_weight(credibility_tier: int | None) -> float:
    if credibility_tier is None:
        return UNKNOWN_TIER_WEIGHT
    return TIER_WEIGHTS.get(credibility_tier, UNKNOWN_TIER_WEIGHT)


def weigh(docs: list[WeighedDoc]) -> list[float]:
    """Effective weight per doc = tier-weight x relevance, with the
    per-domain independence discount applied in input order (callers sort
    deterministically: best tier first, then document id)."""
    seen_domains: set[str] = set()
    weights: list[float] = []
    for doc in docs:
        w = tier_weight(doc.credibility_tier) * doc.relevance
        if doc.domain in seen_domains:
            w *= INDEPENDENCE_DISCOUNT
        else:
            seen_domains.add(doc.domain)
        weights.append(w)
    return weights


def compute_verdict(stances: list[tuple[str, float]],
                    ) -> tuple[str, float, float, float]:
    """(verdict, confidence, S, total_weight) from weighted stances.

    S = (Σw_supports − Σw_refutes) / Σw_all; the +0.5/−0.5 mixed terms of
    the engine formula cancel exactly, but mixed weight still dilutes S
    through the denominator. Confidence = weight-coverage x agreement.
    """
    w_sup = sum(w for s, w in stances if s == "supports")
    w_ref = sum(w for s, w in stances if s == "refutes")
    total = sum(w for s, w in stances if s in ("supports", "refutes", "mixed"))
    if total < MIN_TOTAL_WEIGHT:
        return "unverified", 0.0, 0.0, total
    s_score = (w_sup - w_ref) / total
    if s_score >= SUPPORTED_THRESHOLD:
        verdict = "supported"
    elif s_score <= REFUTED_THRESHOLD:
        verdict = "refuted"
    else:
        verdict = "mixed"
    coverage = min(1.0, total / CONFIDENCE_WEIGHT_SCALE)
    agreement = abs(s_score) if verdict != "mixed" else 1.0 - abs(s_score)
    confidence = round(coverage * agreement, 4)
    return verdict, confidence, round(s_score, 4), round(total, 4)


def doc_domain(url: str | None, source_name: str | None,
               document_id: int) -> str:
    """Publisher-domain key for the independence discount."""
    if url:
        netloc = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if netloc:
            return netloc
    if source_name:
        return f"source:{source_name.lower()}"
    return f"doc:{document_id}"


# -- evidence gathering ------------------------------------------------------------------


async def _doc_row(conn: psycopg.AsyncConnection,
                   doc_id: int) -> _Row | None:
    cur = await conn.execute(
        "SELECT d.id, d.title, d.url, d.content_text, d.visibility,"
        " d.owner_id, s.name AS source_name,"
        " s.credibility_tier FROM document d"
        " LEFT JOIN source s ON s.id = d.source_id WHERE d.id = %s",
        (doc_id,))
    return await cur.fetchone()


async def _url_in_corpus(ctx: AnalysisContext, url: str) -> int | None:
    cur = await ctx.conn.execute(
        "SELECT id FROM document WHERE (url = %s OR canonical_url = %s)"
        " AND (visibility = 'shared' OR owner_id = %s)"
        " LIMIT 1", (url, url, ctx.viewer))
    row = await cur.fetchone()
    return int(row["id"]) if row else None


def _visible_to_viewer(ctx: AnalysisContext, row: _Row) -> bool:
    """The retrieval seam's tenancy rule (design §5): the dossier owner is
    the viewer — shared docs plus their own private docs."""
    return (row["visibility"] == "shared"
            or row["owner_id"] == ctx.viewer)


async def gather_evidence(ctx: AnalysisContext, claim: DecomposedClaim,
                          k: int) -> list[_Row]:
    """Up to ``k`` candidate docs: corpus FTS, then corpus vectors, then
    NEW web docs fetched through the ingestion pipeline. Dedup by doc id;
    fetch failures are skipped, never fatal. The corpus legs run as the
    dossier OWNER (viewer): shared docs + their own private docs."""
    ordered: list[int] = []
    seen: set[int] = set()

    def _take(doc_id: int) -> None:
        if doc_id not in seen:
            seen.add(doc_id)
            ordered.append(doc_id)

    try:
        # ranking only — ids; the snippet/ts_headline pass never runs here
        for doc_id in await fts_dao.rank_documents(
                ctx.conn, claim.text, limit=CORPUS_POOL_LIMIT,
                viewer=ctx.viewer):
            _take(doc_id)
    except psycopg.Error:
        log.exception("corpus FTS failed for claim %s", claim.id)

    if ctx.vectors is not None and len(ordered) < k:
        vecs = ctx.embedder.embed([claim.text])
        if vecs:
            for doc_id, _sim in await ctx.vectors.search(
                    ctx.conn, vecs[0], k=CORPUS_POOL_LIMIT):
                _take(doc_id)

    if len(ordered) < k:
        await _gather_web(ctx, claim, k, _take, len(ordered))

    out = []
    for doc_id in ordered[:k]:
        row = await _doc_row(ctx.conn, doc_id)
        if row is None or not (row["content_text"] or "").strip():
            continue
        # the vector leg ranks over ALL embeddings — re-check visibility
        if not _visible_to_viewer(ctx, row):
            continue
        out.append(row)
    return out


async def _gather_web(ctx: AnalysisContext, claim: DecomposedClaim, k: int,
                      take: Callable[[int], None], have: int) -> None:
    try:
        hits = await ctx.search.search(claim.text, max_results=k)
    except Exception as e:  # noqa: BLE001 — search failure degrades to corpus
        log.warning("web search failed for claim %s: %s", claim.id, e)
        return
    budget = k - have
    for hit in hits:
        if budget <= 0:
            break
        existing = await _url_in_corpus(ctx, hit.url)
        if existing is not None:
            take(existing)
            budget -= 1
            continue
        if ctx.ingest is None:
            continue
        try:
            result = await ctx.ingest.ingest_url(ctx.conn, hit.url)
        except Exception as e:  # noqa: BLE001 — one bad URL never aborts
            log.warning("evidence fetch failed (%s): %s", hit.url, e)
            continue
        take(result.document.id)
        budget -= 1


# -- stance classification -----------------------------------------------------------------


def _stance_user(claim_text: str, doc: _Row) -> str:
    words = (doc["content_text"] or "").split()
    body = " ".join(words[:STANCE_MAX_CONTENT_WORDS])
    truncated = " [truncated]" if len(words) > STANCE_MAX_CONTENT_WORDS \
        else ""
    return (f"CLAIM: {claim_text}\n\n"
            f"DOCUMENT (title: {doc['title'] or '(untitled)'}):\n"
            f"{body}{truncated}")


async def stance_one(ctx: AnalysisContext, claim_text: str,
                     doc: _Row) -> StanceJudgment | None:
    """One claim x doc judgment, span-verified; one retry with the failure
    shown, then discarded. Returns None for discards and 'unrelated'."""
    user = _stance_user(claim_text, doc)
    content = doc["content_text"] or ""
    try:
        completion = await ctx.call_structured(
            system=STANCE_SYSTEM, user=user, schema=StanceJudgment,
            tier=ModelTier.FAST, max_tokens=STANCE_MAX_OUTPUT_TOKENS,
            est_in=EST_STANCE_IN, est_out=EST_STANCE_OUT)
    except LLMError as e:
        log.warning("stance call failed (doc %s): %s", doc["id"], e)
        return None
    judgment = completion.output
    if judgment.stance == "unrelated":
        return None
    if grounding.span_is_verbatim(judgment.quoted_span, content):
        return judgment

    retry_user = (f"{user}\n\nPREVIOUS ATTEMPT REJECTED: quoted_span was "
                  f"not a verbatim substring of the document text "
                  f"(got: {judgment.quoted_span!r}). Copy the load-bearing "
                  f"sentence EXACTLY as it appears, or return stance "
                  f"'unrelated'.")
    try:
        completion = await ctx.call_structured(
            system=STANCE_SYSTEM, user=retry_user, schema=StanceJudgment,
            tier=ModelTier.FAST, max_tokens=STANCE_MAX_OUTPUT_TOKENS,
            est_in=EST_STANCE_IN, est_out=EST_STANCE_OUT)
    except LLMError as e:
        log.warning("stance retry failed (doc %s): %s", doc["id"], e)
        return None
    judgment = completion.output
    if judgment.stance == "unrelated":
        return None
    if grounding.span_is_verbatim(judgment.quoted_span, content):
        return judgment
    log.warning("stance discarded after retry (doc %s): span not verbatim",
                doc["id"])
    return None


# -- reasoning (model explains AFTER the enum is fixed) ---------------------------------------


def template_reasoning(verdict: str, stances: list[tuple[str, float]],
                       s_score: float) -> str:
    n_sup = sum(1 for s, _ in stances if s == "supports")
    n_ref = sum(1 for s, _ in stances if s == "refutes")
    n_mix = sum(1 for s, _ in stances if s == "mixed")
    return (f"Deterministic verdict '{verdict}': {n_sup} supporting,"
            f" {n_ref} refuting, {n_mix} mixed source(s);"
            f" weighted score {s_score:+.2f}.")


async def write_reasoning(ctx: AnalysisContext, *, claim_text: str,
                          verdict: str, menu: grounding.EvidenceMenu,
                          fallback: str) -> str:
    """BALANCED explanation constrained to the menu; off-menu citations
    reject the output (one retry), any failure falls back to the
    deterministic template — reasoning never blocks a verdict."""
    if not menu.entries:
        return fallback
    base_user = (f"CLAIM: {claim_text}\n"
                 f"FIXED VERDICT: {verdict}\n\n"
                 f"EVIDENCE MENU:\n{menu.render()}")
    user = base_user
    for attempt in range(2):
        try:
            completion = await ctx.call_structured(
                system=REASONING_SYSTEM, user=user, schema=ClaimReasoning,
                tier=ModelTier.BALANCED,
                max_tokens=REASONING_MAX_OUTPUT_TOKENS,
                est_in=EST_REASONING_IN, est_out=EST_REASONING_OUT)
        except (LLMError, BudgetExceeded, AnalysisBudgetExceeded) as e:
            log.warning("reasoning call failed: %s", e)
            return fallback
        bad = menu.invalid_ids(completion.output.cited_evidence_ids)
        if not bad:
            return completion.output.reasoning
        log.warning("reasoning cited off-menu ids %s (attempt %s)",
                    bad, attempt + 1)
        user = (f"{base_user}\n\nPREVIOUS ATTEMPT REJECTED: you cited "
                f"ids not on the menu: {', '.join(bad)}. Cite ONLY the "
                f"listed evidence ids.")
    return fallback


# -- per-claim budget plan ----------------------------------------------------------------------


def plan_claim_k(ctx: AnalysisContext, k: int) -> tuple[int | None, bool]:
    """(k_to_use | None=abort, degraded?) — degrade K first, then abort."""
    def projected(n_docs: int) -> float:
        return (n_docs * ctx.projected_cost(ModelTier.FAST, EST_STANCE_IN,
                                            EST_STANCE_OUT)
                + ctx.projected_cost(ModelTier.BALANCED, EST_REASONING_IN,
                                     EST_REASONING_OUT))
    if ctx.budget.fits(projected(k)):
        return k, False
    degraded = min(DEGRADED_K, k)
    if ctx.budget.fits(projected(degraded)):
        return degraded, True
    return None, True


# -- the stage -----------------------------------------------------------------------------------


async def run(ctx: AnalysisContext, normalized: NormalizedInput,
              save: Callable[[list[ClaimResult], str | None],
                             Awaitable[None] | None] | None = None,
              ) -> tuple[list[ClaimResult], str]:
    """Verify every checkable claim; returns (claim results, summary).

    ``save`` (when given) persists the in-progress results after
    reconciliation and after every claim — the snapshot the SSE-reconnect
    contract relies on.
    """
    conn = ctx.conn
    results: list[ClaimResult] = []

    # reconcile ALL claims onto canonical nodes first (ids exist while
    # verification is still running). Gate 2 (I1): a PRIVATE dossier never
    # touches the shared claim table — its claims get synthetic NEGATIVE
    # ids and live only in the dossier-scoped section content.
    for index, claim in enumerate(normalized.claims):
        if ctx.shared:
            claim_id, method = await writeback.reconcile_claim(
                ctx, text=claim.text, kind=claim.kind)
        else:
            claim_id, method = -(index + 1), "private"
        results.append(ClaimResult(
            claim_id=claim_id, local_id=claim.id, text=claim.text,
            kind=claim.kind, checkable=claim.checkable,
            note=None if claim.checkable else "not checkable",
        ))
        await ctx.emit("stage_progress",
                 {"stage": "verify",
                  "message": f"claim {claim.id} -> #{claim_id} ({method})"})
    if save is not None:
        _maybe_await = save(list(results), None)
        if asyncio.iscoroutine(_maybe_await):
            await _maybe_await

    k_default = ctx.max_evidence_per_claim
    verified = degraded_any = 0
    aborted = False
    checkable = [(i, c) for i, c in enumerate(normalized.claims)
                 if c.checkable]

    for index, claim in checkable:
        if aborted:
            results[index] = results[index].model_copy(update={
                "note": "skipped: analysis budget exhausted"})
            continue
        k_claim, degraded = plan_claim_k(ctx, k_default)
        if k_claim is None:
            aborted = True
            results[index] = results[index].model_copy(update={
                "note": "skipped: analysis budget exhausted"})
            await ctx.emit("stage_progress",
                     {"stage": "verify",
                      "message": f"budget exhausted before {claim.id};"
                                 f" aborting with partial result"})
            continue
        if degraded:
            degraded_any += 1
            await ctx.emit("stage_progress",
                     {"stage": "verify",
                      "message": f"budget low: K degraded to {k_claim}"
                                 f" for {claim.id}"})

        results[index] = await _verify_claim(
            ctx, claim, results[index], k_claim, degraded=degraded)
        verified += 1
        await ctx.emit("claim_verified",
                       {"claim_id": results[index].claim_id,
                        "verdict": results[index].verdict})
        if save is not None:
            _maybe_await = save(list(results), None)
            if asyncio.iscoroutine(_maybe_await):
                await _maybe_await

    # the contradiction scan reads the shared evidence ledger; a private
    # dossier wrote nothing there, so there is nothing new to scan
    new_contradictions = await contradictions.scan(conn) if ctx.shared else 0

    notes = [f"verified {verified} of {len(checkable)} checkable claims"]
    if ctx.search.name == "null":
        notes.append("corpus-only: TAVILY_API_KEY not set")
    if degraded_any:
        notes.append(f"budget: K degraded for {degraded_any} claim(s)")
    if aborted:
        notes.append("budget: aborted with partial result")
    if new_contradictions:
        notes.append(f"contradictions: {new_contradictions} open")
    summary = "; ".join(notes)
    return results, summary


async def _verify_claim(ctx: AnalysisContext, claim: DecomposedClaim,
                        base: ClaimResult, k: int, *,
                        degraded: bool) -> ClaimResult:
    docs = await gather_evidence(ctx, claim, k)
    await ctx.emit("stage_progress",
                   {"stage": "verify",
                    "message": f"{claim.id}: {len(docs)} evidence docs"})

    judgments_raw = await asyncio.gather(
        *(stance_one(ctx, claim.text, doc) for doc in docs),
        return_exceptions=True)
    accepted: list[tuple[_Row, StanceJudgment]] = []
    budget_hit = False
    for doc, judgment in zip(docs, judgments_raw):
        if isinstance(judgment, (BudgetExceeded, AnalysisBudgetExceeded)):
            budget_hit = True
            continue
        if isinstance(judgment, BaseException):
            log.warning("stance task failed (doc %s): %s",
                        doc["id"], judgment)
            continue
        if judgment is not None:
            accepted.append((doc, judgment))

    # deterministic weighting order: best tier first, then doc id
    accepted.sort(key=lambda pair: (pair[0]["credibility_tier"] or 5,
                                    pair[0]["id"]))
    weighed_docs = [WeighedDoc(
        document_id=doc["id"],
        domain=doc_domain(doc["url"], doc["source_name"], doc["id"]),
        credibility_tier=doc["credibility_tier"],
        stance=j.stance, relevance=j.relevance,
    ) for doc, j in accepted]
    weights = weigh(weighed_docs)
    stances = [(wd.stance, w) for wd, w in zip(weighed_docs, weights)]
    verdict, confidence, s_score, total_weight = compute_verdict(stances)

    # KB writeback: evidence rows (grade 2) + closed menu for reasoning.
    # Gate 2 (I1): rows are written only for SHARED dossiers, and evidence
    # referencing a PRIVATE document is dropped from the shared writeback
    # (synthetic negative id; the quote survives in the dossier-scoped
    # section content only).
    menu = grounding.EvidenceMenu()
    evidence: list[EvidenceRef] = []
    for (doc, judgment), weight in zip(accepted, weights):
        if ctx.shared and doc["visibility"] == "shared":
            evidence_id = await writeback.write_evidence(
                ctx.conn, claim_id=base.claim_id, document_id=doc["id"],
                stance=judgment.stance, relevance=judgment.relevance,
                quote=judgment.quoted_span, note=judgment.note,
                model=ctx.provider.model_for(ModelTier.FAST))
        else:
            evidence_id = -(len(evidence) + 1)
        evidence.append(EvidenceRef(
            evidence_id=evidence_id, document_id=doc["id"],
            stance=judgment.stance, relevance=judgment.relevance,
            weight=round(weight, 4), quote=judgment.quoted_span))
        menu.add(document_id=doc["id"], quote=judgment.quoted_span,
                 source_name=doc["source_name"],
                 credibility_tier=doc["credibility_tier"],
                 stance=judgment.stance)

    fallback = template_reasoning(verdict, stances, s_score)
    reasoning = await write_reasoning(
        ctx, claim_text=claim.text, verdict=verdict, menu=menu,
        fallback=fallback)

    if ctx.shared:
        # snapshot only the PERSISTED evidence rows (positive ids) — a
        # dropped private-doc ref must not enter the shared verdict ledger
        await writeback.update_verdict(
            ctx.conn, claim_id=base.claim_id, verdict=verdict,
            confidence=confidence, dossier_id=ctx.dossier_id,
            evidence=[e for e in evidence if e.evidence_id > 0])

    note_parts = []
    if degraded:
        note_parts.append(f"K degraded to {k}")
    if budget_hit:
        note_parts.append("some stance calls skipped (budget)")
    return base.model_copy(update={
        "verdict": verdict, "confidence": confidence, "score": s_score,
        "total_weight": total_weight, "reasoning": reasoning,
        "evidence": evidence,
        "note": "; ".join(note_parts) or None,
    })
