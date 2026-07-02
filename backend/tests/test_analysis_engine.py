"""Phase 3 engine internals: credibility weighting (table-driven from the
engine-doc numbers), verdict thresholds, span-verification discard path,
closed evidence menus, claim reconciliation merge paths, the contradiction
scan SQL, and per-claim budget degrade/abort. NO network, MockProvider
only."""

from __future__ import annotations

import pytest
from kb_factories import insert_doc, insert_source
from mock_llm import MockProvider

from connect.analysis import grounding, writeback
from connect.analysis.budget import AnalysisBudget
from connect.analysis.context import AnalysisContext
from connect.analysis.schema import (
    ClaimReasoning,
    DecomposedClaim,
    NormalizedInput,
    SameProposition,
    StanceJudgment,
)
from connect.analysis.stages import verify
from connect.knowledge import contradictions
from connect.knowledge.embedder import NullEmbedder
from connect.llm.spend import Governor
from connect.retrieval.search_client import NullSearchClient
from connect.storage.pg import Vector, utc_now
from dbutil import q1, qv
from kb_factories import pad


class FakeEmbedder:
    model_name = "fake"
    dim = 384

    def __init__(self, mapping: dict[str, list[float]]):
        self.mapping = mapping

    def embed(self, texts):
        return [pad(self.mapping[t]) for t in texts if t in self.mapping]


async def _noop_emit(t, d):
    return 0


def make_ctx(pool, conn, provider=None, *, cap=2.0, k=6, embedder=None,
             search=None, emit=None):
    return AnalysisContext(
        conn=conn, provider=provider or MockProvider(),
        governor=Governor(pool, 100.0), budget=AnalysisBudget(cap),
        search=search or NullSearchClient(), ingest=None,
        embedder=embedder or NullEmbedder(), vectors=None, dossier_id=1,
        max_evidence_per_claim=k, emit=emit or _noop_emit)


# --- weighting math (engine design 2c: 1.0/0.8/0.5/0.3, unknown 0.2, --------------
# --- x relevance, per-domain independence discount 0.3x after the first) ----------

@pytest.mark.parametrize("tier,expected", [
    (1, 1.0), (2, 0.8), (3, 0.5), (4, 0.3), (None, 0.2), (9, 0.2)])
def test_tier_weights(tier, expected):
    assert verify.tier_weight(tier) == expected


@pytest.mark.parametrize("docs,expected", [
    # one doc per domain: tier weight x relevance, no discount
    ([(1, "a.com", 1.0), (2, "b.com", 1.0)], [1.0, 0.8]),
    # relevance multiplies
    ([(1, "a.com", 0.5)], [0.5]),
    ([(3, "c.com", 0.4)], [0.2]),
    # second doc from the SAME domain: x0.3
    ([(2, "et.com", 1.0), (2, "et.com", 1.0)], [0.8, 0.24]),
    # third from the same domain discounts too; other domains unaffected
    ([(2, "et.com", 1.0), (1, "pib.gov.in", 1.0), (2, "et.com", 0.5)],
     [0.8, 1.0, 0.12]),
    # unknown tier doc still gets domain-discounted
    ([(None, "x.com", 1.0), (None, "x.com", 1.0)], [0.2, 0.06]),
])
def test_weigh_table(docs, expected):
    weighed = [verify.WeighedDoc(document_id=i, domain=d, stance="supports",
                                 credibility_tier=t, relevance=r)
               for i, (t, d, r) in enumerate(docs)]
    got = verify.weigh(weighed)
    assert [round(w, 6) for w in got] == [round(w, 6) for w in expected]


def test_doc_domain_extraction():
    assert verify.doc_domain("https://www.thehindu.com/x/y", None, 1) == \
        "thehindu.com"
    assert verify.doc_domain("http://pib.gov.in/rel", None, 1) == \
        "pib.gov.in"
    assert verify.doc_domain(None, "PIB", 7) == "source:pib"
    assert verify.doc_domain(None, None, 7) == "doc:7"


# --- verdict formula + thresholds ---------------------------------------------------

def test_verdict_supported():
    # S = (1.8 - 0.8) / 2.6 = 0.3846 >= 0.3
    verdict, confidence, s, total = verify.compute_verdict(
        [("supports", 1.0), ("supports", 0.8), ("refutes", 0.8)])
    assert verdict == "supported"
    assert s == pytest.approx(0.3846, abs=1e-4)
    assert total == pytest.approx(2.6)
    assert 0 < confidence <= 1


def test_verdict_refuted():
    verdict, _, s, _ = verify.compute_verdict(
        [("refutes", 1.0), ("supports", 0.2)])
    assert verdict == "refuted"
    assert s < verify.REFUTED_THRESHOLD


def test_verdict_mixed_band_and_mixed_dilution():
    # equal support/refute -> S = 0 -> mixed
    verdict, _, s, _ = verify.compute_verdict(
        [("supports", 1.0), ("refutes", 1.0)])
    assert (verdict, s) == ("mixed", 0.0)
    # mixed stance dilutes S through the denominator only
    verdict, _, s, total = verify.compute_verdict(
        [("supports", 1.0), ("mixed", 1.0)])
    assert s == pytest.approx(0.5)
    assert total == pytest.approx(2.0)
    assert verdict == "supported"


def test_verdict_min_weight_gate():
    # below MIN_TOTAL_WEIGHT (0.5) -> unverified regardless of agreement
    verdict, confidence, s, total = verify.compute_verdict(
        [("supports", 0.49)])
    assert (verdict, confidence, s) == ("unverified", 0.0, 0.0)
    assert total == pytest.approx(0.49)
    verdict, _, _, _ = verify.compute_verdict([("supports", 0.5)])
    assert verdict == "supported"
    assert verify.compute_verdict([])[0] == "unverified"


# --- grounding: span verification + closed menus -------------------------------------

def test_span_is_verbatim_whitespace_normalized():
    text = "The RBI raised the\n  repo rate by 25 bps."
    assert grounding.span_is_verbatim("RBI raised the repo rate", text)
    assert grounding.span_is_verbatim("raised the\nrepo  rate", text)
    assert not grounding.span_is_verbatim("RBI cut the repo rate", text)
    assert not grounding.span_is_verbatim("", text)


def test_evidence_menu_rejects_off_menu_ids():
    menu = grounding.EvidenceMenu()
    e1 = menu.add(document_id=10, quote="quote one", source_name="PIB",
                  credibility_tier=1, stance="supports")
    menu.add(document_id=11, quote="quote two")
    assert e1.menu_id == "E1"
    assert menu.ids == {"E1", "E2"}
    assert menu.invalid_ids(["E1", "E9", "bogus"]) == ["E9", "bogus"]
    assert menu.invalid_ids(["E1", "E2"]) == []
    rendered = menu.render()
    assert "[E1] (PIB, tier 1, supports)" in rendered
    assert "quote two" in rendered


def test_evidence_menu_render_lines_matches_render():
    """render() is exactly the joined render_lines() — the planner's
    entry-aligned truncation depends on this — and tier 0 renders as a real
    tier, not 'tier unknown'."""
    menu = grounding.EvidenceMenu()
    menu.add(document_id=1, quote="q1", source_name="PIB", credibility_tier=0)
    menu.add(document_id=2, quote="q2")
    assert menu.render() == "\n".join(menu.render_lines())
    assert "(PIB, tier 0)" in menu.render_lines()[0]
    assert "tier unknown" in menu.render_lines()[1]


# --- stance: span-verification discard path -------------------------------------------

async def test_stance_discard_after_failed_retry(container, db):
    conn = db
    doc_id = await insert_doc(conn, title="RBI",
                              text="The RBI raised the repo "
                              "rate by 25 basis points on Friday.")
    doc = await verify._doc_row(conn, doc_id)
    bad = StanceJudgment(stance="supports", quoted_span="totally invented",
                         relevance=0.9, note="")
    provider = MockProvider(respond=bad)
    ctx = make_ctx(container.pool, conn, provider)
    assert await verify.stance_one(ctx, "RBI raised rates", doc) is None
    # exactly one retry happened, with the failure shown
    assert len(provider.calls) == 2
    assert "PREVIOUS ATTEMPT REJECTED" in provider.calls[1]["user_text"]


async def test_stance_retry_recovers(container, db):
    conn = db
    doc_id = await insert_doc(conn, title="RBI",
                              text="The RBI raised the repo "
                              "rate by 25 basis points on Friday.")
    doc = await verify._doc_row(conn, doc_id)
    answers = iter([
        StanceJudgment(stance="supports", quoted_span="invented",
                       relevance=0.9, note=""),
        StanceJudgment(stance="supports",
                       quoted_span="raised the repo rate by 25 basis points",
                       relevance=0.9, note=""),
    ])
    provider = MockProvider(respond=lambda _user: next(answers))
    ctx = make_ctx(container.pool, conn, provider)
    judgment = await verify.stance_one(ctx, "RBI raised rates", doc)
    assert judgment is not None
    assert judgment.quoted_span.startswith("raised the repo rate")


async def test_stance_unrelated_discarded(container, db):
    conn = db
    doc_id = await insert_doc(conn, text="Entirely about cricket scores.")
    doc = await verify._doc_row(conn, doc_id)
    provider = MockProvider(respond=StanceJudgment(
        stance="unrelated", quoted_span="", relevance=0.0, note=""))
    ctx = make_ctx(container.pool, conn, provider)
    assert await verify.stance_one(ctx, "RBI raised rates", doc) is None
    assert len(provider.calls) == 1  # no retry for honest unrelated


# --- reasoning: menu-constrained, fallback template -------------------------------------

async def test_reasoning_off_menu_falls_back_to_template(container, db):
    conn = db
    menu = grounding.EvidenceMenu()
    menu.add(document_id=1, quote="q1")
    provider = MockProvider(respond=ClaimReasoning(
        reasoning="bogus [E9]", cited_evidence_ids=["E9"]))
    ctx = make_ctx(container.pool, conn, provider)
    out = await verify.write_reasoning(
        ctx, claim_text="c", verdict="supported", menu=menu,
        fallback="TEMPLATE")
    assert out == "TEMPLATE"
    assert len(provider.calls) == 2  # one retry, then fallback
    assert "PREVIOUS ATTEMPT REJECTED" in provider.calls[1]["user_text"]


async def test_reasoning_accepts_on_menu_citations(container, db):
    conn = db
    menu = grounding.EvidenceMenu()
    menu.add(document_id=1, quote="q1")
    provider = MockProvider(respond=ClaimReasoning(
        reasoning="Grounded by [E1].", cited_evidence_ids=["E1"]))
    ctx = make_ctx(container.pool, conn, provider)
    out = await verify.write_reasoning(
        ctx, claim_text="c", verdict="supported", menu=menu, fallback="T")
    assert out == "Grounded by [E1]."


async def test_reasoning_empty_menu_uses_template(container, db):
    ctx = make_ctx(container.pool, db, MockProvider())
    out = await verify.write_reasoning(
        ctx, claim_text="c", verdict="unverified",
        menu=grounding.EvidenceMenu(), fallback="T")
    assert out == "T"


def test_template_reasoning_is_deterministic():
    text = verify.template_reasoning(
        "supported", [("supports", 1.0), ("refutes", 0.3)], 0.54)
    assert "supported" in text and "1 supporting" in text \
        and "1 refuting" in text and "+0.54" in text


# --- claim reconciliation merge paths -------------------------------------------------

async def _seed_claim(conn, text, vec=None):
    cur = await conn.execute(
        "INSERT INTO claim (text, created_at) VALUES (%s, %s) RETURNING id",
        (text, utc_now()))
    claim_id = int((await cur.fetchone())["id"])
    if vec is not None:
        await conn.execute(
            "INSERT INTO claim_embedding (claim_id, model, embedding)"
            " VALUES (%s, 'fake', %s)", (claim_id, Vector(pad(vec))))
    return claim_id


async def test_reconcile_exact_text_match(container, db):
    conn = db
    existing = await _seed_claim(conn, "The RBI raised the repo rate.")
    ctx = make_ctx(container.pool, conn)
    claim_id, method = await writeback.reconcile_claim(
        ctx, text="the RBI  raised the repo rate.", kind="factual")
    assert (claim_id, method) == (existing, "exact")


async def test_reconcile_vec_merge_above_092(container, db):
    conn = db
    existing = await _seed_claim(conn, "RBI hiked repo by 25 bps",
                                 vec=[1, 0, 0])
    ctx = make_ctx(container.pool, conn, embedder=FakeEmbedder(
        {"Repo rate increased 25 basis points": [1.0, 0.0, 0.0]}))
    claim_id, method = await writeback.reconcile_claim(
        ctx, text="Repo rate increased 25 basis points", kind="factual")
    assert (claim_id, method) == (existing, "vec_merge")


async def test_reconcile_gray_zone_adjudicated_yes(container, db):
    conn = db
    existing = await _seed_claim(conn, "RBI hiked repo by 25 bps",
                                 vec=[1, 0, 0])
    # cosine = 0.85 -> gray zone [0.80, 0.92)
    embedder = FakeEmbedder({"Repo went up 25 points": [0.85, 0.5268, 0.0]})
    provider = MockProvider(
        respond_by_schema={SameProposition: SameProposition(same=True)})
    ctx = make_ctx(container.pool, conn, provider, embedder=embedder)
    claim_id, method = await writeback.reconcile_claim(
        ctx, text="Repo went up 25 points", kind="factual")
    assert (claim_id, method) == (existing, "adjudicated_merge")


async def test_reconcile_gray_zone_adjudicated_no_creates_new(container,
                                                              db):
    conn = db
    existing = await _seed_claim(conn, "RBI hiked repo by 25 bps",
                                 vec=[1, 0, 0])
    embedder = FakeEmbedder({"CRR cut by 50 points": [0.85, 0.5268, 0.0]})
    provider = MockProvider(
        respond_by_schema={SameProposition: SameProposition(same=False)})
    ctx = make_ctx(container.pool, conn, provider, embedder=embedder)
    claim_id, method = await writeback.reconcile_claim(
        ctx, text="CRR cut by 50 points", kind="factual")
    assert claim_id != existing
    assert method == "new"
    # the new claim got its own embedding for future reconciliation
    assert await qv(conn, "SELECT COUNT(*) FROM claim_embedding") == 2


async def test_reconcile_below_080_is_new_without_adjudication(container,
                                                               db):
    conn = db
    await _seed_claim(conn, "RBI hiked repo by 25 bps", vec=[1, 0, 0])
    embedder = FakeEmbedder({"Parliament passed the bill": [0.0, 1.0, 0.0]})
    provider = MockProvider()  # would assert if any call happened
    ctx = make_ctx(container.pool, conn, provider, embedder=embedder)
    claim_id, method = await writeback.reconcile_claim(
        ctx, text="Parliament passed the bill", kind="factual")
    assert method == "new"
    assert provider.calls == []


async def test_reconcile_kind_maps_claim_type(container, db):
    conn = db
    ctx = make_ctx(container.pool, conn)
    claim_id, _ = await writeback.reconcile_claim(
        ctx, text="The govt should cut taxes", kind="normative")
    assert await qv(conn, "SELECT claim_type FROM claim WHERE id = %s",
                    claim_id) == "opinion"


# --- contradiction scan (pure SQL) ----------------------------------------------------

async def _evidence(conn, claim_id, doc_id, stance):
    await conn.execute(
        "INSERT INTO evidence (claim_id, document_id, stance, grade,"
        " created_at) VALUES (%s,%s,%s,2,%s)",
        (claim_id, doc_id, stance, utc_now()))


async def test_contradiction_scan_sql(db):
    conn = db
    tier1 = await insert_source(conn, "PIB", tier=1)
    tier2 = await insert_source(conn, "ET", tier=2)
    d1 = await insert_doc(conn, source_id=tier1)
    d2 = await insert_doc(conn, source_id=tier2)
    d3 = await insert_doc(conn)  # no source -> tier treated as 4
    disputed = await _seed_claim(conn, "Disputed claim")
    onesided = await _seed_claim(conn, "One-sided claim")
    await _evidence(conn, disputed, d1, "supports")
    await _evidence(conn, disputed, d2, "refutes")
    await _evidence(conn, disputed, d3, "supports")
    await _evidence(conn, onesided, d1, "supports")

    assert await contradictions.scan(conn) == 1
    rows, total = await contradictions.list_page(conn)
    assert total == 1
    row = rows[0]
    assert row["claim"]["id"] == disputed
    assert (row["n_support"], row["n_refute"]) == (2, 1)
    assert (row["best_tier_support"], row["best_tier_refute"]) == (1, 2)
    assert row["status"] == "open"

    # rescan refreshes counts without duplicating rows
    d4 = await insert_doc(conn, source_id=tier2)
    await _evidence(conn, disputed, d4, "refutes")
    assert await contradictions.scan(conn) == 1
    rows, total = await contradictions.list_page(conn)
    assert total == 1
    assert (rows[0]["n_support"], rows[0]["n_refute"]) == (2, 2)

    # dismissal is user state — the scanner never reopens it
    dismissed = await contradictions.dismiss(conn, rows[0]["id"])
    assert dismissed["status"] == "dismissed"
    await contradictions.scan(conn)
    assert (await contradictions.get(conn, rows[0]["id"]))["status"] \
        == "dismissed"
    assert await contradictions.dismiss(conn, 99999) is None


# --- evidence gathering: web hits fetched through the EXISTING ingest --------------------

class FakeSearchClient(NullSearchClient):
    name = "fake"

    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    async def search(self, query, max_results=5):
        self.queries.append((query, max_results))
        return self.hits[:max_results]


async def test_gather_evidence_fetches_new_web_docs(container, db):
    from canned_web import FakeFetcher, html_page

    from connect.retrieval.search_client import SearchHit

    conn = db
    url = "https://news.example.com/repo-hike"
    container.pipeline.fetcher = FakeFetcher({
        url: html_page("RBI raises repo rate",
                       "<p>The RBI raised the repo rate by 25 basis "
                       "points, the central bank said.</p>")})
    search = FakeSearchClient([SearchHit(url=url, title="RBI raises")])
    ctx = make_ctx(container.pool, conn, MockProvider(), search=search)
    ctx.ingest = container.pipeline

    claim = DecomposedClaim(id="C1", text="RBI raised the repo rate",
                            kind="factual", checkable=True)
    docs = await verify.gather_evidence(ctx, claim, k=3)
    assert search.queries == [("RBI raised the repo rate", 3)]
    assert len(docs) == 1
    assert docs[0]["url"] == url
    assert "repo rate" in docs[0]["content_text"]
    # the snapshot landed through the normal ingest (immutable corpus)
    assert await qv(conn, "SELECT COUNT(*) FROM document WHERE url = %s",
                    url) == 1
    # second gather reuses the stored doc instead of refetching
    docs2 = await verify.gather_evidence(ctx, claim, k=3)
    assert [d["id"] for d in docs2] == [docs[0]["id"]]
    assert container.pipeline.fetcher.calls == [url]


# --- budget: degrade K, then abort-with-partial ------------------------------------------

async def test_plan_claim_k_degrades_then_aborts(container, db):
    conn = db
    ctx = make_ctx(container.pool, conn, MockProvider(), cap=2.0, k=6)
    full = (6 * ctx.projected_cost(
        verify.ModelTier.FAST, verify.EST_STANCE_IN, verify.EST_STANCE_OUT)
        + ctx.projected_cost(verify.ModelTier.BALANCED,
                             verify.EST_REASONING_IN,
                             verify.EST_REASONING_OUT))
    degraded = (verify.DEGRADED_K * ctx.projected_cost(
        verify.ModelTier.FAST, verify.EST_STANCE_IN, verify.EST_STANCE_OUT)
        + ctx.projected_cost(verify.ModelTier.BALANCED,
                             verify.EST_REASONING_IN,
                             verify.EST_REASONING_OUT))
    # plenty of budget -> full K
    assert verify.plan_claim_k(ctx, 6) == (6, False)
    # between degraded and full -> K degrades
    ctx.budget = AnalysisBudget((full + degraded) / 2)
    assert verify.plan_claim_k(ctx, 6) == (verify.DEGRADED_K, True)
    # below even the degraded shape -> abort signal
    ctx.budget = AnalysisBudget(degraded / 2)
    assert verify.plan_claim_k(ctx, 6) == (None, True)


async def test_verify_run_budget_abort_partial(container, db):
    """First claim verifies (degraded K), the second is skipped when the
    budget is exhausted — partial result, summary says so."""
    conn = db
    src = await insert_source(conn, "ET", tier=2)
    await insert_doc(conn, title="RBI raised repo rate",
                     text="The RBI raised the repo rate by 25 basis"
                          " points.",
                     source_id=src)

    def stance(_user):
        return StanceJudgment(
            stance="supports",
            quoted_span="raised the repo rate by 25 basis points",
            relevance=1.0, note="")

    provider = MockProvider(
        respond=stance,
        respond_by_schema={
            ClaimReasoning: ClaimReasoning(reasoning="ok",
                                           cited_evidence_ids=[]),
        })
    ctx = make_ctx(container.pool, conn, provider, cap=0.02, k=6)
    normalized = NormalizedInput(
        subject="rbi", input_kind="news_claim",
        claims=[
            DecomposedClaim(id="C1", text="RBI raised the repo rate",
                            kind="factual", checkable=True),
            DecomposedClaim(id="C2", text="RBI raised repo rate again",
                            kind="factual", checkable=True),
        ])
    results, summary = await verify.run(ctx, normalized)
    assert results[0].verdict is not None
    assert "K degraded" in (results[0].note or "")
    assert results[1].verdict is None
    assert results[1].note == "skipped: analysis budget exhausted"
    assert "aborted with partial result" in summary
    assert "corpus-only: TAVILY_API_KEY not set" in summary
