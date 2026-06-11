"""P3 retrieval layer: RRF fusion math, the hybrid document search
(tsvector ranking + pgvector KNN), its lexical-only degradation, snippet
escape discipline on vector-only hits, and the claim tsvector path.
Synthetic vectors only (no model, no network)."""

from __future__ import annotations

from connect.knowledge.embedder import NullEmbedder
from connect.knowledge.vector import DisabledIndex, PgVectorIndex
from connect.retrieval import search as retrieval
from connect.retrieval.search import hybrid_document_ids, rrf_fuse
from connect.storage import fts as fts_dao
from connect.storage.pg import utc_now
from kb_factories import insert_doc, pad, set_vector


class FakeEmbedder:
    """Fixed query->vector mapping; unknown text embeds to nothing."""

    model_name = "fake"
    dim = 384

    def __init__(self, mapping: dict[str, list[float]]):
        self.mapping = mapping

    def embed(self, texts):
        return [pad(self.mapping[t]) for t in texts if t in self.mapping]


# --- RRF fusion math --------------------------------------------------------


def test_rrf_orders_by_reciprocal_rank_sum():
    # doc 3: ranks 1st + 2nd; doc 1: ranks 2nd + 1st (tie, broken by id);
    # doc 9 appears in one list only -> last
    fused = rrf_fuse([[3, 1, 9], [1, 3]])
    assert fused == [1, 3, 9]


def test_rrf_single_list_is_identity():
    assert rrf_fuse([[5, 2, 7]]) == [5, 2, 7]
    assert rrf_fuse([[5, 2, 7], []]) == [5, 2, 7]
    assert rrf_fuse([]) == []


def test_rrf_k_dampens_rank_gaps():
    # with a huge k every appearance weighs ~equally: two-list membership
    # beats any single-list rank
    fused = rrf_fuse([[1, 2, 3], [3]], k=10_000)
    assert fused[0] == 3


# --- hybrid document ids ------------------------------------------------------


async def _corpus(db):
    """Three docs: lexical hit (far vector), pure-vector hit (near vector,
    hostile markup up front), and an unrelated far-vector doc."""
    d_lex = await insert_doc(
        db, title="Trade report",
        text="A report on zanzibar trade routes and spice tariffs.")
    d_vec = await insert_doc(
        db, title="Procurement note",
        text="<script>alert(1)</script> Grain & oilseed procurement "
             "prices were revised upward for the coming season.")
    d_far = await insert_doc(
        db, title="Unrelated",
        text="Cricket league fixtures were announced for the winter.")
    await set_vector(db, d_lex, [0.0, 1.0, 0.0])
    await set_vector(db, d_vec, [1.0, 0.0, 0.0])
    await set_vector(db, d_far, [0.0, 0.0, 1.0])
    return d_lex, d_vec, d_far


async def test_hybrid_fuses_lexical_and_vector(db):
    d_lex, d_vec, _d_far = await _corpus(db)
    embedder = FakeEmbedder({"zanzibar": [1.0, 0.0, 0.0]})
    fused = await hybrid_document_ids(
        db, "zanzibar", embedder=embedder, vectors=PgVectorIndex())
    # lexical rank 1 + a vector rank beats the vector-only top hit
    assert fused[0] == d_lex
    assert d_vec in fused


async def test_hybrid_degrades_lexical_only(db):
    d_lex, _d_vec, _d_far = await _corpus(db)
    lexical = await fts_dao.rank_documents(db, "zanzibar", limit=50)
    # no index wired at all
    assert await hybrid_document_ids(
        db, "zanzibar", embedder=None, vectors=None) == lexical == [d_lex]
    # embeddings off: NullEmbedder produces no vector, DisabledIndex empty
    assert await hybrid_document_ids(
        db, "zanzibar", embedder=NullEmbedder(),
        vectors=DisabledIndex()) == lexical
    # real index, but the query embeds to nothing
    assert await hybrid_document_ids(
        db, "zanzibar", embedder=FakeEmbedder({}),
        vectors=PgVectorIndex()) == lexical


async def test_hybrid_vector_only_when_no_lexical_match(db):
    _d_lex, d_vec, _d_far = await _corpus(db)
    embedder = FakeEmbedder({"granary stocks": [1.0, 0.0, 0.0]})
    fused = await hybrid_document_ids(
        db, "granary stocks", embedder=embedder, vectors=PgVectorIndex())
    assert fused[0] == d_vec  # nearest neighbour leads; KNN returns all 3
    assert len(fused) == 3


# --- the search surface -------------------------------------------------------


async def test_search_hybrid_page_snippets_and_totals(db):
    d_lex, d_vec, _d_far = await _corpus(db)
    embedder = FakeEmbedder({"zanzibar": [1.0, 0.0, 0.0]})
    result = await retrieval.search(db, "zanzibar", kind="documents",
                                    embedder=embedder,
                                    vectors=PgVectorIndex())
    ids = [d.id for d in result.documents]
    assert ids[0] == d_lex and d_vec in ids
    # all three embedded docs surface (KNN k >> corpus) and the total
    # reflects what fusion surfaced, never less than the page
    assert result.total_documents == result.total == len(ids) == 3

    by_id = {d.id: d for d in result.documents}
    # lexical hit keeps the ranked-match snippet with <mark> highlights
    assert "<mark>zanzibar</mark>" in by_id[d_lex].snippet
    # the vector-only hit gets the leading fragment — under the SAME
    # escape discipline: raw document markup never reaches the client
    snip = by_id[d_vec].snippet
    assert snip
    assert "<script>" not in snip
    assert "&amp;" in snip and "&" not in snip.replace("&amp;", "")


async def test_search_without_vectors_is_v01_lexical(db):
    d_lex, _d_vec, _d_far = await _corpus(db)
    result = await retrieval.search(db, "zanzibar", kind="documents")
    items, total = await fts_dao.search_documents(db, "zanzibar")
    assert [d.id for d in result.documents] == [i.id for i in items] \
        == [d_lex]
    assert result.total_documents == result.total == total == 1
    assert result.documents[0].snippet == items[0].snippet


async def test_search_junk_query_never_raises(db):
    await _corpus(db)
    result = await retrieval.search(db, '"AND OR (((', kind="all")
    assert result.total_documents == 0 and result.documents == []


# --- claim tsvector path --------------------------------------------------------


async def _claim(db, text):
    cur = await db.execute(
        "INSERT INTO claim (text, created_at) VALUES (%s, %s) RETURNING id",
        (text, utc_now()))
    return int((await cur.fetchone())["id"])


async def test_search_claims_ranked_websearch(db):
    c1 = await _claim(db, "The RBI raised the repo rate by 25 basis points")
    await _claim(db, "Parliament passed the appropriation bill")
    hits = await fts_dao.search_claims(db, "repo rate")
    assert [h[0] for h in hits] == [c1]
    assert "repo rate" in hits[0][1]
    # websearch_to_tsquery tolerates arbitrary input — never raises
    assert await fts_dao.search_claims(db, '"AND OR (((') == []
