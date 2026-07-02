"""S2 — the entailment scorer (analysis/entailment.py)."""

from __future__ import annotations

from mock_llm import MockProvider

from connect.analysis.budget import AnalysisBudget
from connect.analysis.entailment import EntailmentJudgment, score


class _Gov:
    async def check(self, *a, **k):
        return None


def _provider(label: str) -> MockProvider:
    return MockProvider(respond_by_schema={
        EntailmentJudgment: lambda _u: EntailmentJudgment(
            label=label, rationale="r")})


async def test_score_returns_label_and_records_spend(pg_conn):
    prov = _provider("not_entailed")
    j = await score(pg_conn, prov, evidence="Inflation eased to 4.8%.",
                    statement="Inflation soared to 19%.",
                    budget=AnalysisBudget(1.0), governor=_Gov(), viewer=None)
    assert j.label == "not_entailed"
    # the call was recorded under the content_verify purpose
    cur = await pg_conn.execute(
        "SELECT count(*) AS n FROM llm_call WHERE purpose = 'content_verify'")
    assert (await cur.fetchone())["n"] == 1


async def test_score_entailed(pg_conn):
    j = await score(pg_conn, _provider("entailed"),
                    evidence="The RBI held the repo rate at 6.5%.",
                    statement="The RBI kept rates at 6.5%.",
                    budget=AnalysisBudget(1.0), governor=_Gov(), viewer=None)
    assert j.label == "entailed"
