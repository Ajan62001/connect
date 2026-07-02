"""The editorial planner (content/plan.py) — the 'auto' campaign orchestrator.

The LLM proposes; _sanitize disposes: format vocabulary enforced
(case-insensitive), picks deduped and capped by significance, stock_video
downgraded off non-reels / when unavailable, text formats forced to no media,
memes dropped from big news, the reel moved to the front of a big commission,
free-text fields bounded, and the significance label + big_news flag derived
from the score in code. A planner LLM failure degrades to a one-card fallback
plan; a budget denial still propagates.
"""

from __future__ import annotations

import re

import pytest
from mock_llm import MockProvider

from connect.analysis.budget import AnalysisBudget, AnalysisBudgetExceeded
from connect.content import plan as plan_mod
from connect.content.plan import EditorialPlan, FormatPick, plan_content
from connect.llm.provider import LLMError
from connect.story.schema import StoryFact, StoryFactSet


class _Gov:
    async def check(self, *a, **k):
        return None


def _factset() -> StoryFactSet:
    return StoryFactSet(subject="RBI repo rate decision", facts=[
        StoryFact(document_id=1, quote="The RBI kept the repo rate at 6.5%.",
                  title="RBI holds", source_name="RBI", credibility_tier=1)])


def _provider(plan: EditorialPlan) -> MockProvider:
    return MockProvider(respond_by_schema={EditorialPlan: lambda _u: plan})


async def _plan(conn, plan: EditorialPlan, *,
                video_available: bool = True) -> EditorialPlan:
    return await plan_content(
        conn, _provider(plan), factset=_factset(),
        budget=AnalysisBudget(1.0), governor=_Gov(), viewer=None,
        video_available=video_available)


async def test_big_news_commissions_multiple_formats(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=5, rationale="market-moving rate decision",
        angle="6.5% again — fourth straight hold",
        picks=[FormatPick(format="ig_reel", reason="travels",
                          media="stock_video", media_query="RBI building"),
               FormatPick(format="x_thread", reason="fast", media="none"),
               FormatPick(format="ig_card", reason="feed",
                          media="stock_photo", media_query="rupee cash")]))
    assert [p.format for p in out.picks] == ["ig_reel", "x_thread", "ig_card"]
    assert out.picks[0].media == "stock_video"
    assert out.big_news is True
    assert out.significance_label == "breaking"


async def test_unknown_and_duplicate_formats_dropped(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="tiktok", reason="not a format"),
               FormatPick(format="ig_card", reason="ok"),
               FormatPick(format="ig_card", reason="dup")]))
    assert [p.format for p in out.picks] == ["ig_card"]


async def test_picks_capped_at_four(pg_conn):
    fmts = ["ig_reel", "ig_card", "ig_carousel", "x_thread", "linkedin_post",
            "meme"]
    out = await _plan(pg_conn, EditorialPlan(
        significance=5, rationale="r",
        picks=[FormatPick(format=f, reason="r") for f in fmts]))
    assert len(out.picks) == 4
    assert [p.format for p in out.picks] == fmts[:4]


async def test_empty_picks_fall_back_to_card(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=2, rationale="r", picks=[]))
    assert [p.format for p in out.picks] == ["ig_card"]


async def test_stock_video_downgraded_when_unavailable(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=4, rationale="r",
        picks=[FormatPick(format="ig_reel", reason="r",
                          media="stock_video")]),
        video_available=False)
    assert out.picks[0].media == "stock_photo"


async def test_stock_video_downgraded_on_non_reel(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r",
                          media="stock_video")]))
    assert out.picks[0].media == "stock_photo"


async def test_unknown_media_kind_coerced_to_photo(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r", media="gif")]))
    assert out.picks[0].media == "stock_photo"


async def test_big_news_flag_derived_from_score_not_model(pg_conn):
    # the model contradicting itself (score 5 but big_news False, label '')
    # cannot leak through — both are recomputed from the score.
    out = await _plan(pg_conn, EditorialPlan(
        significance=5, big_news=False, significance_label="tiny",
        rationale="r", picks=[FormatPick(format="ig_card", reason="r")]))
    assert out.big_news is True
    assert out.significance_label == "breaking"
    low = await _plan(pg_conn, EditorialPlan(
        significance=2, big_news=True, significance_label="breaking",
        rationale="r", picks=[FormatPick(format="ig_card", reason="r")]))
    assert low.big_news is False
    assert low.significance_label == "minor"


async def test_pick_count_capped_by_significance(pg_conn):
    # notable (3) commissions at most 2 formats; minor (2) exactly one
    notable = await _plan(pg_conn, EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r"),
               FormatPick(format="x_thread", reason="r"),
               FormatPick(format="linkedin_post", reason="r")]))
    assert [p.format for p in notable.picks] == ["ig_card", "x_thread"]
    minor = await _plan(pg_conn, EditorialPlan(
        significance=2, rationale="r",
        picks=[FormatPick(format="meme", reason="r"),
               FormatPick(format="ig_card", reason="r")]))
    assert [p.format for p in minor.picks] == ["meme"]


async def test_meme_dropped_from_big_news(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=5, rationale="r",
        picks=[FormatPick(format="meme", reason="r"),
               FormatPick(format="ig_card", reason="r")]))
    assert [p.format for p in out.picks] == ["ig_card"]


async def test_meme_always_gets_a_photo(pg_conn):
    # the photo IS the joke — a 'none' treatment on a meme is coerced back
    out = await _plan(pg_conn, EditorialPlan(
        significance=2, rationale="r",
        picks=[FormatPick(format="meme", reason="r", media="none",
                          media_query="office desk")]))
    assert out.picks[0].media == "stock_photo"
    assert out.picks[0].media_query == "office desk"


async def test_reel_survives_the_budget_cut_on_big_news(pg_conn):
    # the reel is sorted to the front BEFORE the pick budget is applied —
    # a model that lists it last must not get it cut instead of leading
    fmts = ["meme", "ig_card", "ig_carousel", "x_thread", "linkedin_post",
            "ig_reel"]
    out = await _plan(pg_conn, EditorialPlan(
        significance=5, rationale="r",
        picks=[FormatPick(format=f, reason="r") for f in fmts]))
    assert [p.format for p in out.picks] == \
        ["ig_reel", "ig_card", "ig_carousel", "x_thread"]


async def test_citation_markers_scrubbed_from_free_text(pg_conn):
    # the planner's menu is renumbered vs the generation menu, so an echoed
    # E# would mis-cite downstream — every marker-ish token is scrubbed
    out = await _plan(pg_conn, EditorialPlan(
        significance=3, rationale="see [[E2]]",
        angle="lead with the [[E3]] 40% jump",
        picks=[FormatPick(format="ig_card", reason="cites [E1]",
                          media_query="E2 rupee [[E4]] cash")]))
    assert out.angle == "lead with the 40% jump"
    assert out.rationale == "see"
    assert out.picks[0].reason == "cites"
    assert out.picks[0].media_query == "rupee cash"


async def test_generation_factset_never_reordered(pg_conn):
    # the planner sorts a LOCAL view newest-first; the factset object that
    # generation will consume must come back in its original order
    prov = _provider(EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r")]))
    factset = StoryFactSet(subject="S", facts=[
        StoryFact(document_id=1, quote="older", occurred_on="2026-01-05"),
        StoryFact(document_id=2, quote="newer", occurred_on="2026-07-01")])
    before = [f.quote for f in factset.facts]
    await plan_content(pg_conn, prov, factset=factset,
                       budget=AnalysisBudget(1.0), governor=_Gov(),
                       viewer=None, video_available=True)
    assert [f.quote for f in factset.facts] == before == ["older", "newer"]


async def test_reel_moved_to_front_of_big_commission(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=4, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r"),
               FormatPick(format="x_thread", reason="r"),
               FormatPick(format="ig_reel", reason="r",
                          media="stock_video")]))
    assert [p.format for p in out.picks] == ["ig_reel", "ig_card", "x_thread"]
    assert out.picks[0].media == "stock_video"


async def test_text_formats_forced_to_no_media(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=4, rationale="r",
        picks=[FormatPick(format="x_thread", reason="r",
                          media="stock_photo", media_query="rupee cash")]))
    assert out.picks[0].media == "none"
    assert out.picks[0].media_query == ""


async def test_format_names_case_insensitive(pg_conn):
    out = await _plan(pg_conn, EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format=" IG_Reel ", reason="r")]))
    assert out.picks[0].format == "ig_reel"


async def test_free_text_fields_bounded(pg_conn):
    # every free-text field is interpolated into generation prompts / the UI
    # — collapsed to one line and capped, media_query to a few plain words
    out = await _plan(pg_conn, EditorialPlan(
        significance=3, angle="a" * 999,
        rationale="line one\n\nline two " + "b" * 999,
        picks=[FormatPick(
            format="ig_card", reason="c" * 999,
            media_query="one two three four five six seven eight")]))
    assert len(out.angle) <= 300
    assert len(out.rationale) <= 500 and "\n" not in out.rationale
    assert len(out.picks[0].reason) <= 200
    assert out.picks[0].media_query == "one two three four five six"


async def test_planner_failure_degrades_to_fallback_plan(pg_conn):
    def _boom(_u):
        raise LLMError("anthropic call failed: 529")

    prov = MockProvider(respond_by_schema={EditorialPlan: _boom})
    out = await plan_content(
        pg_conn, prov, factset=_factset(), budget=AnalysisBudget(1.0),
        governor=_Gov(), viewer=None, video_available=True)
    assert [p.format for p in out.picks] == ["ig_card"]
    assert out.significance_label == "notable"
    assert out.big_news is False
    assert "planner unavailable" in out.rationale


async def test_planner_budget_denial_still_propagates(pg_conn):
    # only LLM failures degrade — an exhausted budget must fail the run
    prov = _provider(EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r")]))
    with pytest.raises(AnalysisBudgetExceeded):
        await plan_content(pg_conn, prov, factset=_factset(),
                           budget=AnalysisBudget(0.0), governor=_Gov(),
                           viewer=None, video_available=True)
    assert prov.calls == []


async def test_planner_prompt_carries_signals_newest_first(pg_conn):
    prov = _provider(EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r")]))
    factset = StoryFactSet(subject="RBI", facts=[
        StoryFact(document_id=1, quote="the old fact", source_name="PIB",
                  credibility_tier=2, occurred_on="2026-01-05"),
        StoryFact(document_id=2, quote="the new fact", source_name="RBI",
                  credibility_tier=1, occurred_on="2026-07-01")])
    await plan_content(pg_conn, prov, factset=factset,
                       budget=AnalysisBudget(1.0), governor=_Gov(),
                       viewer=None, video_available=True)
    user = prov.calls[0]["user_text"]
    assert "TODAY: " in user
    assert "DATES: evidence dated 2026-01-05 to 2026-07-01" in user
    assert "SOURCES: 2 facts from 2 outlet(s)" in user
    assert "showing 2 of 2 facts" in user
    assert user.index("the new fact") < user.index("the old fact")


async def test_menu_truncated_at_entry_boundaries(pg_conn, monkeypatch):
    monkeypatch.setattr(plan_mod, "MENU_CHAR_BUDGET", 200)
    prov = _provider(EditorialPlan(
        significance=3, rationale="r",
        picks=[FormatPick(format="ig_card", reason="r")]))
    facts = [StoryFact(document_id=i, quote=f"fact {i} " + "detail " * 10,
                       source_name="PIB", credibility_tier=1)
             for i in range(1, 9)]
    await plan_content(pg_conn, prov,
                       factset=StoryFactSet(subject="S", facts=facts),
                       budget=AnalysisBudget(1.0), governor=_Gov(),
                       viewer=None, video_available=True)
    user = prov.calls[0]["user_text"]
    m = re.search(r"showing (\d+) of 8 facts", user)
    assert m is not None and 0 < int(m.group(1)) < 8
    menu_block = user.split("facts):\n", 1)[1].rsplit("\n\nCommission", 1)[0]
    lines = menu_block.splitlines()
    assert len(lines) == int(m.group(1))
    for ln in lines:   # entry-aligned: every shown line is a whole entry
        assert ln.startswith("[E") and ln.endswith('"')
