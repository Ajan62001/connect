"""ToolExecutor gate behavior: web_search is FREE (never gated by the
corpus_only budget flip) while fetch_and_ingest stays gated. Constructed
directly — these handlers touch only state + the search client, so the DB
conn / embedder / scope_pack are unused for the gated paths."""

from __future__ import annotations

from connect.investigation.tools import InvestigationState, ToolExecutor
from connect.llm.provider import ToolOutcome
from connect.retrieval.search_client import (
    NullSearchClient,
    SearchClient,
    SearchHit,
)


class FakeSearch(SearchClient):
    name = "fake"

    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, max_results=5):
        return list(self._hits)[:max_results]


async def _noop_emit(_kind, _data):
    return None


def _executor(*, search):
    return ToolExecutor(
        None, pipeline=None, search=search, embedder=None, vectors=None,
        scope_pack=None, state=InvestigationState(dossier_id=1),
        emit=_noop_emit)


async def test_web_search_not_gated_by_corpus_only():
    ex = _executor(search=FakeSearch(
        [SearchHit(url="https://x.example/a", title="A", snippet="s")]))
    ex.state.corpus_only = True  # budget degraded
    result = await ex._tool_web_search({"query": "rbi repo rate"})
    # a free search still runs under budget pressure: a dict of results, not
    # an is_error ToolOutcome
    assert not isinstance(result, ToolOutcome)
    assert result["results"][0]["url"] == "https://x.example/a"


async def test_web_search_unavailable_when_null_client():
    ex = _executor(search=NullSearchClient())
    out = await ex._tool_web_search({"query": "x"})
    assert isinstance(out, ToolOutcome) and out.is_error
    assert "unavailable" in out.content


async def test_fetch_and_ingest_still_gated_by_corpus_only():
    ex = _executor(search=NullSearchClient())
    ex.state.corpus_only = True
    out = await ex._tool_fetch_and_ingest({"url": "https://x.example/a"})
    assert isinstance(out, ToolOutcome) and out.is_error
    assert "corpus-only" in out.content
