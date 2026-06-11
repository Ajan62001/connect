"""The manual investigation loop (design §2.3) with MockProvider scripted
tool turns: findings recorded + question lifecycle, the degrade ladder
(60% corpus-only flips web tools to is_error, 85% forced conclude), forced
conclude on max_iterations and on the daily governor, end_turn nudge,
cancel mid-loop, and per-turn 'investigation' ledger rows."""

from __future__ import annotations

import asyncio

from dbutil import q1, qall, qv
from kb_factories import insert_doc
from mock_llm import MockProvider, tool_turn

from connect.investigation.runner import InvestigationService
from connect.investigation.schema import (
    GeneratedQuestion,
    GeneratedQuestions,
    InvestigationOptions,
    InvestigationSeed,
    SynthesisOutput,
)
from connect.llm.provider import Usage
from connect.llm.spend import INVESTIGATION_PURPOSES, Governor

DOC_TEXT = ("The RBI tightened the rules in response to the payment fraud "
            "wave reported in May. Industry groups objected.")
QUOTE = ("The RBI tightened the rules in response to the payment fraud "
         "wave reported in May.")

QGEN = GeneratedQuestions(questions=[GeneratedQuestion(
    qtype="what_triggered", text="What triggered the tightening?",
    priority=0.9)])
SYNTH = SynthesisOutput(narrative_md="Nothing notable.", chains=[],
                        actors=[], watch_next=[])


def make_provider(turns, *, usage=None) -> MockProvider:
    kwargs = {"respond_by_schema": {GeneratedQuestions: QGEN,
                                    SynthesisOutput: SYNTH},
              "tool_turns": turns}
    if usage is not None:
        kwargs["usage"] = usage
    return MockProvider(**kwargs)


def make_service(container, provider, *, daily_budget=10.0,
                 reserve=0.0) -> InvestigationService:
    service = InvestigationService(
        container.pool, jobs=container.jobs, provider=provider,
        governor=Governor(container.pool, daily_budget,
                          purposes=INVESTIGATION_PURPOSES),
        search=container.search_client, ingest=container.pipeline,
        embedder=container.embedder, vectors=container.vectors,
        synthesis_reserve_usd=reserve, synthesis_tier="balanced")
    # the registered 'investigation' handler dispatches through the
    # composition root — point it at THIS service (the closure used to
    # capture it; the registry resolves it from services instead)
    container.investigations = service
    return service


async def run_to_completion(container, service, seed, options):
    dossier_id, job_id = await service.start(seed, options)
    await asyncio.gather(*list(container.jobs._tasks),
                         return_exceptions=True)
    return dossier_id, job_id


async def events_of(conn, job_id):
    return [(r["type"], r["data"] or {}) for r in await qall(
        conn,
        "SELECT type, data FROM job_event WHERE job_id = %s ORDER BY seq",
        job_id)]


def tools_calls(provider):
    return [c for c in provider.calls if c.get("kind") == "tools"]


# --- happy path: findings + question lifecycle ------------------------------------


async def test_loop_records_findings_and_resolves_questions(container,
                                                             db):
    conn = db
    doc_id = await insert_doc(conn, title="RBI tightens rules",
                              text=DOC_TEXT)
    turns = [
        tool_turn(("search_corpus", {"query": "rbi rules"})),
        tool_turn(("record_finding", {
            "kind": "trigger",
            "text": "The tightening was triggered by the fraud wave.",
            "evidence": [{"document_id": doc_id, "quote": QUOTE}],
            "speculation": False, "question_id": 1, "confidence": 0.8})),
        tool_turn(("conclude", {
            "summary": "Fraud wave triggered the tightening.",
            "resolutions": [{"question_id": 1, "status": "answered",
                             "answer_summary": "the fraud wave",
                             "finding_ids": [1]}],
            "confidence": 0.8})),
    ]
    provider = make_provider(turns)
    service = make_service(container, provider)
    dossier_id, job_id = await run_to_completion(
        container, service, InvestigationSeed(topic="rbi rules"),
        InvestigationOptions(budget_usd=1.0, max_iterations=10))

    row = await q1(conn, "SELECT * FROM dossier WHERE id = %s",
                   dossier_id)
    assert row["status"] == "completed"
    assert row["kind"] == "investigation"
    assert row["input_type"] == "topic"

    # question lifecycle: open (generated) -> partial (finding) -> answered
    q = await q1(conn, "SELECT * FROM question WHERE id = 1")
    assert q["status"] == "answered"
    assert q["answer_summary"] == "the fraud wave"
    assert q["answer_finding_ids"] == [1]

    finding = await q1(conn, "SELECT * FROM finding WHERE id = 1")
    assert finding["kind"] == "trigger"
    assert await qv(conn, "SELECT quote FROM finding_evidence WHERE"
                          " finding_id = 1") == QUOTE

    # SSE rows: question_raised -> iteration* -> finding_recorded ->
    # question_resolved -> section_completed* -> done
    events = await events_of(conn, job_id)
    names = [t for t, _ in events]
    for expected in ("question_raised", "iteration", "finding_recorded",
                     "question_resolved", "section_completed", "done"):
        assert expected in names
    assert names.index("question_raised") < names.index("finding_recorded")
    assert names.index("finding_recorded") < names.index(
        "question_resolved")
    iter_events = [d for t, d in events if t == "iteration"]
    assert [e["n"] for e in iter_events] == [1, 2, 3]
    assert iter_events[0]["tools"] == ["search_corpus"]
    assert iter_events[-1]["cost_so_far"] > 0

    # every loop turn ledgered under the investigation purpose
    assert await qv(
        conn,
        "SELECT COUNT(*) FROM llm_call WHERE purpose = 'investigation'",
    ) == 4  # 1 question-gen + 3 loop turns
    assert await qv(
        conn, "SELECT COUNT(*) FROM llm_call WHERE purpose ="
              " 'investigation_synthesis'") == 1

    # all sections persisted
    stages = {r["stage"]: r["status"] for r in await qall(
        conn,
        "SELECT stage, status FROM dossier_section WHERE dossier_id = %s",
        dossier_id)}
    for stage in ("scope", "investigate", "synthesize", "timeline",
                  "causal_narrative", "actors", "alternatives",
                  "open_questions", "watch_next"):
        assert stages[stage] == "completed", stage

    # invalid quote rejection counter stays zero on the happy path
    investigate = await qv(
        conn,
        "SELECT content FROM dossier_section WHERE dossier_id = %s"
        " AND stage = 'investigate'", dossier_id)
    assert investigate["concluded"] is True
    assert investigate["rejected_findings"] == 0


# --- degrade ladder ------------------------------------------------------------------


async def test_degrade_ladder_corpus_only_then_forced_conclude(container,
                                                                db):
    conn = db
    await insert_doc(conn, title="doc", text=DOC_TEXT)
    # expensive turns: ~$0.15 each on sonnet (40k in / 2k out)
    usage = Usage(input_tokens=40_000, output_tokens=2_000)

    def scripted(record):
        if record["tool_choice"] == "conclude":
            return tool_turn(("conclude", {
                "summary": "stopped by budget", "resolutions": [],
                "confidence": 0.2}), usage=usage)
        n = sum(1 for c in provider.calls if c.get("kind") == "tools")
        if n == 1:
            return tool_turn(("search_corpus", {"query": "x"}),
                             usage=usage)
        return tool_turn(("web_search", {"query": "x"}), usage=usage)

    provider = make_provider(scripted, usage=usage)
    service = make_service(container, provider)
    dossier_id, job_id = await run_to_completion(
        container, service, InvestigationSeed(topic="budget drill"),
        InvestigationOptions(budget_usd=0.50, max_iterations=10))

    assert await qv(conn, "SELECT status FROM dossier WHERE id = %s",
                    dossier_id) == "completed"
    iter_events = [d for t, d in await events_of(conn, job_id)
                   if t == "iteration"]
    # qgen ($0.15, 30%) + turn1 -> 60% flips corpus_only before turn 2,
    # 85% forces conclude on turn 3
    assert [e["corpus_only"] for e in iter_events] == [False, True, True]
    assert [e["forced"] for e in iter_events] == [False, False, True]

    calls = tools_calls(provider)
    assert [c["tool_choice"] for c in calls] == [None, None, "conclude"]
    # turn 2's web_search came back as an is_error tool_result naming the
    # corpus-only degrade (visible to the model in turn 3's transcript)
    final_messages = calls[2]["messages"]
    block = final_messages[-1]["content"][0]
    assert block["type"] == "tool_result"
    assert block["is_error"] is True
    assert "corpus-only" in block["content"]


async def test_forced_conclude_on_max_iterations(container, db):
    conn = db

    def scripted(record):
        if record["tool_choice"] == "conclude":
            return tool_turn(("conclude", {
                "summary": "out of iterations", "resolutions": [],
                "confidence": 0.3}))
        return tool_turn(("search_corpus", {"query": "again"}))

    provider = make_provider(scripted)
    service = make_service(container, provider)
    dossier_id, job_id = await run_to_completion(
        container, service, InvestigationSeed(topic="iteration drill"),
        InvestigationOptions(budget_usd=5.0, max_iterations=3))

    calls = tools_calls(provider)
    assert len(calls) == 3
    assert [c["tool_choice"] for c in calls] == [None, None, "conclude"]
    assert await qv(conn, "SELECT status FROM dossier WHERE id = %s",
                    dossier_id) == "completed"
    investigate = await qv(
        conn,
        "SELECT content FROM dossier_section WHERE dossier_id = %s"
        " AND stage = 'investigate'", dossier_id)
    assert investigate["concluded"] is True
    assert investigate["iterations"] == 3


async def test_daily_governor_exhaustion_forces_conclude(container, db):
    """The SEPARATE daily governor trips mid-run -> one last forced
    conclude instead of a crash."""
    conn = db

    def scripted(record):
        if record["tool_choice"] == "conclude":
            return tool_turn(("conclude", {"summary": "daily cap",
                                           "resolutions": [],
                                           "confidence": 0.1}))
        return tool_turn(("search_corpus", {"query": "x"}))

    provider = make_provider(scripted)
    # daily investigation budget so small the first loop turn cannot fit
    service = make_service(container, provider, daily_budget=0.001)
    dossier_id, _job_id = await run_to_completion(
        container, service, InvestigationSeed(topic="governor drill"),
        InvestigationOptions(budget_usd=1.0, max_iterations=5))

    calls = tools_calls(provider)
    assert calls[0]["tool_choice"] == "conclude"  # forced from turn 1
    assert await qv(conn, "SELECT status FROM dossier WHERE id = %s",
                    dossier_id) == "completed"


async def test_end_turn_without_tools_forces_conclude_next(container,
                                                            db):
    conn = db

    def scripted(record):
        if record["tool_choice"] == "conclude":
            return tool_turn(("conclude", {"summary": "ok",
                                           "resolutions": [],
                                           "confidence": 0.5}))
        return tool_turn(text="I think I am done.")  # no tool calls

    provider = make_provider(scripted)
    service = make_service(container, provider)
    dossier_id, _job_id = await run_to_completion(
        container, service, InvestigationSeed(topic="endturn drill"),
        InvestigationOptions(budget_usd=1.0, max_iterations=5))

    calls = tools_calls(provider)
    assert [c["tool_choice"] for c in calls] == [None, "conclude"]
    # the nudge message is in the forced turn's transcript
    nudge = calls[1]["messages"][-1]["content"]
    assert "conclude" in nudge
    assert await qv(conn, "SELECT status FROM dossier WHERE id = %s",
                    dossier_id) == "completed"


# --- cancel mid-loop ------------------------------------------------------------------


class HangingToolsProvider(MockProvider):
    async def complete_with_tools(self, **kwargs):
        await asyncio.sleep(30)
        return await super().complete_with_tools(**kwargs)


async def test_cancel_mid_loop(container, db):
    conn = db
    provider = HangingToolsProvider(
        respond_by_schema={GeneratedQuestions: QGEN,
                           SynthesisOutput: SYNTH})
    service = make_service(container, provider)
    dossier_id, job_id = await service.start(
        InvestigationSeed(topic="cancel drill"),
        InvestigationOptions(budget_usd=1.0))

    for _ in range(200):  # let the job start and hang on the first turn
        await asyncio.sleep(0.01)
        row = await q1(conn, "SELECT status FROM dossier WHERE id = %s",
                       dossier_id)
        if row["status"] == "running" and provider.calls:
            break
    assert row["status"] == "running"

    assert await service.cancel(dossier_id) is True
    await asyncio.gather(*list(container.jobs._tasks),
                         return_exceptions=True)
    assert await qv(conn, "SELECT status FROM dossier WHERE id = %s",
                    dossier_id) == "cancelled"
    assert await qv(conn, "SELECT status FROM job WHERE id = %s",
                    job_id) == "cancelled"
    # cancelling a terminal run is a no-op
    assert await service.cancel(dossier_id) is False


# --- rejected finding surfaces as is_error and is counted ------------------------------


async def test_invalid_quote_round_trips_as_is_error(container, db):
    conn = db
    doc_id = await insert_doc(conn, title="doc", text=DOC_TEXT)

    def scripted(record):
        if record["tool_choice"] == "conclude":
            return tool_turn(("conclude", {"summary": "done",
                                           "resolutions": [],
                                           "confidence": 0.4}))
        n = sum(1 for c in provider.calls if c.get("kind") == "tools")
        if n == 1:
            return tool_turn(("record_finding", {
                "kind": "context", "text": "bad quote",
                "evidence": [{"document_id": doc_id,
                              "quote": "this sentence is not in the doc"}],
            }))
        return tool_turn(("record_finding", {
            "kind": "context", "text": "good quote",
            "evidence": [{"document_id": doc_id, "quote": QUOTE}]}))

    provider = make_provider(scripted)
    service = make_service(container, provider)
    dossier_id, _job_id = await run_to_completion(
        container, service, InvestigationSeed(topic="retry drill"),
        InvestigationOptions(budget_usd=1.0, max_iterations=3))

    calls = tools_calls(provider)
    rejection = calls[1]["messages"][-1]["content"][0]
    assert rejection["is_error"] is True
    assert "not a verbatim substring" in rejection["content"]
    # the retry (turn 2) landed; only ONE finding row exists
    assert await qv(conn, "SELECT COUNT(*) FROM finding") == 1
    investigate = await qv(
        conn,
        "SELECT content FROM dossier_section WHERE dossier_id = %s"
        " AND stage = 'investigate'", dossier_id)
    assert investigate["rejected_findings"] == 1
    assert investigate["findings_recorded"] == 1


# --- the generic tool_loop template (provider ABC) --------------------------------------


async def test_tool_loop_template_terminal_and_end_turn():
    from connect.llm.provider import ToolOutcome
    from connect.llm.tiers import ModelTier

    provider = MockProvider(tool_turns=[
        tool_turn(("search_corpus", {"query": "x"})),
        tool_turn(("conclude", {"summary": "done"})),
    ])
    executed = []

    async def executor(call):
        executed.append(call.name)
        return ToolOutcome(content="{}")

    result = await provider.tool_loop(
        system="s", messages=[{"role": "user", "content": "go"}],
        tools=[], executor=executor, tier=ModelTier.BALANCED,
        max_iterations=5)
    assert result.stop == "terminal"
    assert executed == ["search_corpus", "conclude"]
    assert len(result.turns) == 2
    assert result.usage.input_tokens == 4400  # two turns at 2200 each

    # end_turn stop: a text-only turn ends the loop
    provider = MockProvider(tool_turns=[tool_turn(text="all done")])
    result = await provider.tool_loop(
        system="s", messages=[{"role": "user", "content": "go"}],
        tools=[], executor=executor, tier=ModelTier.BALANCED,
        max_iterations=5)
    assert result.stop == "end_turn"
