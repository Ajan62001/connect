"""Synthesis grounding (design §5): the [[f#]] citation checker (unknown
ids -> one regeneration -> strip + flag), watch_next anchoring, chain
speculation inheritance, the BALANCED over-reserve fallback and the
budget-skip path."""

from __future__ import annotations

import pytest
from kb_factories import insert_doc
from mock_llm import MockProvider

from connect.analysis.budget import AnalysisBudget
from connect.investigation import synthesize
from connect.investigation.schema import (
    ActorItem,
    CalendarItem,
    CausalChain,
    ChainStep,
    ScopePack,
    SynthesisOutput,
    WatchNextItem,
)
from connect.investigation.synthesize import (
    check_citations,
    strip_markers,
)
from connect.llm.spend import INVESTIGATION_PURPOSES, Governor
from connect.llm.tiers import DEFAULT_TIER_MODELS, ModelTier
from connect.storage.pg import utc_now
from dbutil import qv


def test_check_citations_and_strip():
    assert check_citations("x [[f1]] y [[f9]] z [[f1]]", {1, 2}) == [
        "[[f9]]"]
    assert check_citations("no markers", {1}) == []
    assert check_citations("", set()) == []
    assert strip_markers("a [[f9]] b [[f9]]", ["[[f9]]"]) == "a  b "


@pytest.fixture()
async def env(db, pool):
    conn = db
    doc_id = await insert_doc(conn, title="doc",
                              text="The committee objected to the draft.")
    await conn.execute(
        "INSERT INTO dossier (kind, input_text, status, created_at)"
        " VALUES ('investigation', 'draft rules', 'running', %s)",
        (utc_now(),))
    await conn.execute(
        "INSERT INTO question (dossier_id, qtype, text, status,"
        " created_at) VALUES (1, 'why_now', 'Why now?', 'open', %s)",
        (utc_now(),))
    # two findings: one grounded, one speculative
    await conn.execute(
        "INSERT INTO finding (dossier_id, kind, text, speculation,"
        " created_at) VALUES (1, 'trigger', 'grounded trigger', FALSE,"
        " %s)",
        (utc_now(),))
    await conn.execute(
        "INSERT INTO finding_evidence (finding_id, document_id, quote)"
        " VALUES (1, %s, 'The committee objected to the draft.')",
        (doc_id,))
    await conn.execute(
        "INSERT INTO finding (dossier_id, kind, text, speculation,"
        " created_at) VALUES (1, 'reaction', 'maybe a reaction', TRUE,"
        " %s)",
        (utc_now(),))
    pack = ScopePack(
        input_text="draft rules", input_type="topic",
        calendar=[CalendarItem(calendar_event_id=5, kind="budget",
                               occurs_on="2026-02-01", label="Budget")])
    return {"conn": conn, "pool": pool, "dossier_id": 1, "pack": pack}


def write_sections(conn):
    written = {}

    async def writer(dossier_id, stage, content, status="completed"):
        written[stage] = {"status": status, "content": content}
    return written, writer


async def run_synthesis(env, provider, *, budget=None, tier_pref="deep",
                        governor=None):
    written, writer = write_sections(env["conn"])
    emitted = []

    async def emit(t, d=None):
        emitted.append((t, d))

    summary = await synthesize.run(
        env["conn"], provider, dossier_id=env["dossier_id"],
        pack=env["pack"], budget=budget or AnalysisBudget(5.0),
        governor=governor or Governor(env["pool"], 10.0,
                                      purposes=INVESTIGATION_PURPOSES),
        emit=emit,
        tier_pref=tier_pref, reserve_usd=0.20, section_writer=writer)
    return summary, written, emitted


GOOD = SynthesisOutput(
    narrative_md="The trigger [[f1]] led to a hypothesised reaction"
                 " [[f2]].",
    chains=[CausalChain(steps=[
        ChainStep(node_type="event", node_id=1, title="A",
                  relation_to_next="triggered_by", finding_id=1),
        ChainStep(node_type="event", node_id=2, title="B", finding_id=2),
    ])],
    actors=[ActorItem(entity_id=1, name="Ministry", role="author",
                      motive_md="Acted on objections [[f1]].",
                      finding_ids=[1, 99])],
    watch_next=[
        WatchNextItem(text="watch the question", question_id=1),
        WatchNextItem(text="watch the budget", calendar_event_id=5),
        WatchNextItem(text="anchorless — must drop",
                      watch_suggestion="rbi rules"),
        WatchNextItem(text="bad anchors — must drop", question_id=99,
                      calendar_event_id=99),
    ])


async def test_grounded_output_passes_first_try(env):
    provider = MockProvider(respond_by_schema={SynthesisOutput: GOOD})
    summary, written, emitted = await run_synthesis(env, provider)

    narrative = written["causal_narrative"]["content"]
    assert narrative["narrative_md"] == GOOD.narrative_md  # untouched
    report = narrative["grounding_report"]
    assert report == {"stripped_citations": [], "regenerated": False,
                      "dropped_watch_items": 2}

    # chain inherits speculation from the speculative finding f2
    chain = narrative["chains"][0]
    assert chain["speculation"] is True
    assert chain["steps"][0]["speculation"] is False
    assert chain["steps"][1]["speculation"] is True

    # actors: off-menu finding id 99 filtered, real ones kept
    actor = written["actors"]["content"]["actors"][0]
    assert actor["finding_ids"] == [1]

    # watch_next: anchored items kept, anchorless/bad-anchor dropped
    items = written["watch_next"]["content"]["items"]
    assert [i["text"] for i in items] == ["watch the question",
                                          "watch the budget"]

    # code sections persisted too
    assert written["open_questions"]["content"]["items"][0][
        "question_id"] == 1
    assert "timeline" in written and "alternatives" in written
    assert {t for t, _ in emitted} == {"section_completed"}
    # the one structured call was DEEP and ledgered under synthesis
    assert provider.calls[0]["tier"] is ModelTier.DEEP
    assert await qv(env["conn"],
                    "SELECT COUNT(*) FROM llm_call WHERE"
                    " purpose='investigation_synthesis'") == 1
    assert "2 findings synthesized on deep tier" in summary


async def test_actor_entity_ids_resolved_not_trusted(env):
    """Live-run regression: the DEEP call never sees the entity table, so
    its actor entity ids are fabricated. They must be resolved by
    name/alias; a model id survives only when its entity name is
    consistent with the actor's; everything else nulls out."""
    conn = env["conn"]
    for name, aliases in (("Reserve Bank of India", '["RBI"]'),
                          ("Federation of Digital Lenders", "[]"),
                          ("Arjun Malpani", "[]")):
        await conn.execute(
            "INSERT INTO entity (name, aliases, created_at)"
            " VALUES (%s, %s, %s)", (name, aliases, utc_now()))
    resolve = synthesize._resolve_actor_entity
    # fabricated id, exact name -> resolved to the real row
    assert await resolve(conn, 99, "Federation of Digital Lenders") == 2
    # parenthetical acronym stripped before matching
    assert await resolve(conn, 1,
                         "Federation of Digital Lenders (FDL)") == 2
    # alias match (case-insensitive)
    assert await resolve(conn, 99, "rbi") == 1
    # supplied id kept when its entity name is consistent with the actor
    assert await resolve(conn, 1, "Reserve Bank of India officials") == 1
    # inconsistent id + unknown name -> None (UI renders unlinked card)
    assert await resolve(conn, 2, "World Bank") is None
    assert await resolve(conn, None, "Nobody Known") is None
    assert await resolve(conn, 99, "") is None


async def test_actor_entity_resolution_through_pipeline(env):
    conn = env["conn"]
    await conn.execute(
        "INSERT INTO entity (name, aliases, created_at)"
        " VALUES ('Finance Ministry', '[\"Ministry\"]', %s)",
        (utc_now(),))
    provider = MockProvider(respond_by_schema={SynthesisOutput: GOOD})
    _, written, _ = await run_synthesis(env, provider)
    # GOOD's actor claims entity_id=1 with name 'Ministry' -> alias hit
    assert written["actors"]["content"]["actors"][0]["entity_id"] == 1


async def test_unknown_citation_regenerated_then_stripped(env):
    bad = GOOD.model_copy(update={
        "narrative_md": "Solid [[f1]] but invented [[f77]]."})
    calls = {"n": 0}

    def responder(_user):
        calls["n"] += 1
        return bad  # still bad on the regeneration

    provider = MockProvider(respond_by_schema={SynthesisOutput: responder})
    _summary, written, _emitted = await run_synthesis(env, provider)

    assert calls["n"] == 2  # one regeneration
    narrative = written["causal_narrative"]["content"]
    assert "[[f77]]" not in narrative["narrative_md"]  # stripped
    assert "[[f1]]" in narrative["narrative_md"]       # kept
    report = narrative["grounding_report"]
    assert report["regenerated"] is True
    assert report["stripped_citations"] == ["[[f77]]"]


async def test_regeneration_fixes_citations(env):
    bad = GOOD.model_copy(update={"narrative_md": "oops [[f77]]"})
    outputs = [bad, GOOD]

    def responder(_user):
        return outputs.pop(0)

    provider = MockProvider(respond_by_schema={SynthesisOutput: responder})
    _summary, written, _ = await run_synthesis(env, provider)
    narrative = written["causal_narrative"]["content"]
    assert narrative["narrative_md"] == GOOD.narrative_md
    assert narrative["grounding_report"]["stripped_citations"] == []
    assert narrative["grounding_report"]["regenerated"] is True


async def test_over_reserve_falls_back_to_balanced(env):
    provider = MockProvider(respond_by_schema={SynthesisOutput: GOOD})
    # opus projection (6000 in/1800 out ~ $0.075) exceeds what is left
    budget = AnalysisBudget(1.0)
    budget.add(0.97)   # remaining 0.03 < deep projection
    summary, written, _ = await run_synthesis(env, provider, budget=budget)
    assert provider.calls[0]["tier"] is ModelTier.BALANCED
    assert "degraded to BALANCED" in summary
    assert "causal_narrative" in written


async def test_budget_skip_persists_code_sections_only(env):
    provider = MockProvider(respond_by_schema={SynthesisOutput: GOOD})
    governor = Governor(env["pool"], 0.0001,
                        purposes=INVESTIGATION_PURPOSES)
    summary, written, _ = await run_synthesis(env, provider,
                                              governor=governor)
    assert "skipped" in summary
    assert written["causal_narrative"]["status"] == "skipped"
    assert written["watch_next"]["status"] == "skipped"
    assert written["timeline"]["status"] == "completed"
    assert written["open_questions"]["status"] == "completed"
    assert provider.calls == []  # no LLM spend at all


async def test_findings_menu_render(env):
    menu = await synthesize.findings_menu(env["conn"], env["dossier_id"])
    assert [m["finding_id"] for m in menu] == [1, 2]
    assert menu[0]["quote"] == "The committee objected to the draft."
    assert menu[1]["speculation"] is True
    rendered = synthesize.render_menu(menu)
    assert "[f1] [trigger]" in rendered
    assert "[f2] [reaction SPECULATIVE]" in rendered


def test_default_models_unchanged():
    # the synthesis tier fallback depends on this routing
    assert DEFAULT_TIER_MODELS[ModelTier.DEEP].startswith("claude-opus")
    assert DEFAULT_TIER_MODELS[ModelTier.BALANCED].startswith(
        "claude-sonnet")


async def test_timeline_section_reads_fresh_edges(env):
    conn = env["conn"]
    await conn.execute("INSERT INTO event (title, occurred_on, created_at)"
                       " VALUES ('A', '2026-05-01', %s)", (utc_now(),))
    await conn.execute("INSERT INTO event (title, occurred_on, created_at)"
                       " VALUES ('B', '2026-05-05', %s)", (utc_now(),))
    await conn.execute(
        "INSERT INTO edge (src_type, src_id, dst_type, dst_id,"
        " relation, properties, grade, created_at) VALUES"
        " ('event', 2, 'event', 1, 'reaction_to',"
        " '{\"speculation\": true}', 2, %s)", (utc_now(),))
    from connect.investigation.schema import TimelineItem
    pack = env["pack"].model_copy(update={"timeline": [
        TimelineItem(event_id=1, date="2026-05-01", title="A"),
        TimelineItem(event_id=2, date="2026-05-05", title="B"),
    ]})
    content = await synthesize.timeline_section(conn, pack)
    by_event = {i["event_id"]: i for i in content["items"]}
    chip_in = by_event[1]["causal"][0]
    chip_out = by_event[2]["causal"][0]
    assert chip_in["direction"] == "in"
    assert chip_out["direction"] == "out"
    assert chip_out["relation"] == "reaction_to"
    assert chip_out["speculation"] is True
    assert chip_out["other_title"] == "A"
