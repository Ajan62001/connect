"""S2 — the publish-time editorial verification gate (content/gate.py).

The gate is deterministic-numeric-coverage first, LLM-entailment second, and
FAILS OPEN: an LLM/budget error records the error and never flags. A corpus
cross-check surfaces cited documents the verdict graph marks contested.
"""

from __future__ import annotations

from mock_llm import MockProvider

from connect.analysis.budget import AnalysisBudget
from connect.analysis.entailment import EntailmentJudgment
from connect.content import gate
from connect.llm.provider import LLMError
from connect.storage.pg import utc_now


class _Gov:
    async def check(self, *a, **k):
        return None


def _provider(label: str = "not_entailed") -> MockProvider:
    return MockProvider(respond_by_schema={
        EntailmentJudgment: lambda _u: EntailmentJudgment(label=label)})


async def _assess(conn, provider, *, texts, quotes, doc_ids=(), enforcing=False):
    return await gate.assess(
        conn, provider, content_texts=list(texts), source_quotes=list(quotes),
        document_ids=list(doc_ids), budget=AnalysisBudget(1.0), governor=_Gov(),
        viewer=None, enforcing=enforcing)


async def test_passes_when_numbers_supported(pg_conn):
    # 6.5 appears in the evidence -> no suspect -> no LLM call -> pass
    report = await _assess(
        pg_conn, _provider("not_entailed"),
        texts=["The RBI held the repo rate at 6.5%."],
        quotes=["The RBI kept the repo rate at 6.5% in June."])
    assert report.verdict == "pass"
    assert report.llm_calls == 0
    assert report.checked == 1


async def test_flags_fabricated_number(pg_conn):
    report = await _assess(
        pg_conn, _provider("not_entailed"),
        texts=["Inflation soared to 19% last month, the data showed."],
        quotes=["Retail inflation eased to 4.8% in May."])
    assert report.verdict == "flagged"
    assert report.llm_calls == 1
    assert report.flagged[0].reason == "not_entailed"


async def test_reexpressed_number_rescued_by_entailment(pg_conn):
    # '2 lakh crore' is not a literal substring of '2,00,000 crore' so the
    # deterministic pass marks it suspect; the LLM says entailed -> not flagged.
    report = await _assess(
        pg_conn, _provider("entailed"),
        texts=["The outlay is 2 lakh crore rupees."],
        quotes=["The government set aside 2,00,000 crore for the scheme."])
    assert report.llm_calls == 1
    assert report.verdict == "pass"


async def test_fails_open_on_provider_error(pg_conn):
    class _Boom(MockProvider):
        async def complete_structured(self, **k):
            raise LLMError("provider down")

    report = await _assess(
        pg_conn, _Boom(),
        texts=["Inflation soared to 19%."],
        quotes=["Retail inflation eased to 4.8% in May."])
    assert report.verdict == "error"        # never 'flagged' on an LLM failure
    assert report.error is not None
    assert report.flagged == []


async def test_oversized_statement_truncates_instead_of_crashing(pg_conn):
    """Regression: GateFinding/ContestedClaim used max_length constraints that
    raised ValidationError (not in _SOFT_ERRORS) — breaking the gate's
    fail-open contract for >600-char sentences. They now truncate."""
    long_sentence = "The quarterly figure moved to 7 " + "x" * 900
    report = await _assess(pg_conn, _provider(), texts=[long_sentence],
                           quotes=[])
    assert report.verdict == "flagged"          # ungrounded, not a crash
    assert len(report.flagged[0].statement) == 600

    finding = gate.GateFinding(statement="s" * 2000, reason="ungrounded",
                               detail="d" * 2000)
    assert len(finding.statement) == 600 and len(finding.detail) == 400
    claim = gate.ContestedClaim(claim_id=1, text="t" * 2000, verdict="refuted")
    assert len(claim.text) == 600


async def test_ungrounded_when_no_quotes(pg_conn):
    report = await _assess(
        pg_conn, _provider(),
        texts=["The RBI cut the repo rate by 50 bps at its meeting today."],
        quotes=[])
    assert report.verdict == "flagged"
    assert any(f.reason == "ungrounded" for f in report.flagged)
    assert report.llm_calls == 0


async def test_contested_sources_surface_refuted_claims(pg_conn):
    cur = await pg_conn.execute(
        "INSERT INTO source (name, type, credibility_tier, created_at)"
        " VALUES ('X','rss',3,%s) RETURNING id", (utc_now(),))
    sid = (await cur.fetchone())["id"]
    cur = await pg_conn.execute(
        "INSERT INTO document (source_id, content_hash, content_text,"
        " fetched_at) VALUES (%s,'h','b',%s) RETURNING id", (sid, utc_now()))
    did = (await cur.fetchone())["id"]
    cur = await pg_conn.execute(
        "INSERT INTO claim (text, verdict, created_at)"
        " VALUES (%s,'refuted',%s) RETURNING id", ("GDP grew 12%", utc_now()))
    claim_id = (await cur.fetchone())["id"]
    await pg_conn.execute(
        "INSERT INTO claim_sighting (claim_id, document_id, stance, created_at)"
        " VALUES (%s,%s,'asserts',%s)", (claim_id, did, utc_now()))

    report = await _assess(
        pg_conn, _provider("entailed"),
        texts=["Growth was strong this quarter."], quotes=["Growth was strong."],
        doc_ids=[did])
    assert len(report.contested) == 1
    assert report.contested[0].verdict == "refuted"


async def test_blocks_only_when_enforcing(pg_conn):
    flagged = await _assess(
        pg_conn, _provider("not_entailed"),
        texts=["Inflation soared to 19%."],
        quotes=["Inflation eased to 4.8%."], enforcing=True)
    assert flagged.verdict == "flagged" and flagged.blocks() is True
    warn = await _assess(
        pg_conn, _provider("not_entailed"),
        texts=["Inflation soared to 19%."],
        quotes=["Inflation eased to 4.8%."], enforcing=False)
    assert warn.verdict == "flagged" and warn.blocks() is False
