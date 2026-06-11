"""Spend ledger math (PRICING, batch discount, cache reads), the Governor,
GET /api/spend, and POST /api/enrichment/sweep job wiring."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mock_llm import MockProvider

from connect.api.main import create_app
from connect.llm import spend
from connect.llm.provider import Usage
from connect.llm.spend import BudgetExceeded, Governor
from connect.llm.tiers import ModelTier, tier_models
from connect.orchestration.config import Settings


@pytest.fixture()
def env(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


# --- pricing math ---------------------------------------------------------------


def test_cost_usd_per_model():
    mtok = dict(input_tokens=1_000_000, output_tokens=1_000_000)
    assert spend.cost_usd("claude-haiku-4-5", **mtok) == 6.0       # 1 + 5
    assert spend.cost_usd("claude-sonnet-4-6", **mtok) == 18.0     # 3 + 15
    assert spend.cost_usd("claude-opus-4-8", **mtok) == 30.0       # 5 + 25
    # batch = 50% off
    assert spend.cost_usd("claude-haiku-4-5", batch=True, **mtok) == 3.0
    # cache reads at 0.1x the input rate
    assert spend.cost_usd("claude-haiku-4-5", input_tokens=0,
                          output_tokens=0,
                          cache_read_tokens=1_000_000) == pytest.approx(0.1)
    # date-suffixed ids resolve by prefix; unknown models use the
    # most-expensive fallback (the governor must never under-count)
    assert spend.price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert spend.price_for("claude-mystery-9") == spend.FALLBACK_PRICE


def test_estimated_t1_cost_matches_design():
    # 2200 in / 300 out on haiku: 0.0022 + 0.0015 = $0.0037 (~design doc)
    assert spend.estimated_t1_cost(
        "claude-haiku-4-5", batch=False) == pytest.approx(0.0037)
    assert spend.estimated_t1_cost(
        "claude-haiku-4-5", batch=True) == pytest.approx(0.00185)


def test_record_call_and_daily_rollups(container):
    conn = container.db
    spend.record_call(conn, purpose="enrich_t1", model="claude-haiku-4-5",
                      usage=Usage(input_tokens=2200, output_tokens=300))
    spend.record_call(conn, purpose="enrich_t1", model="claude-haiku-4-5",
                      usage=Usage(input_tokens=1000, output_tokens=100),
                      batch=True, batch_id="b1")
    rows = conn.execute(
        "SELECT purpose, model, input_tokens, output_tokens, batch_id,"
        " cost_estimate FROM llm_call ORDER BY id").fetchall()
    assert rows[0]["cost_estimate"] == pytest.approx(0.0037)
    assert rows[1]["cost_estimate"] == pytest.approx(0.00075)  # 0.0015 * 0.5
    assert rows[1]["batch_id"] == "b1"

    assert spend.spent_today(conn) == pytest.approx(0.00445)
    assert spend.spent_on(conn, "1999-01-01") == 0.0

    days = spend.daily_breakdown(conn, 3)
    assert len(days) == 3  # zero-filled series, oldest first
    assert days[0]["calls"] == 0 and days[0]["cost_usd"] == 0.0
    today = days[-1]
    assert today["calls"] == 2
    assert today["input_tokens"] == 3200
    assert today["output_tokens"] == 400
    assert today["cost_usd"] == pytest.approx(0.00445)


def test_governor(container):
    conn = container.db
    governor = Governor(conn, daily_budget_usd=1.0)
    governor.check(0.5)  # fine: 0 + 0.5 <= 1.0
    spend.record_call(conn, purpose="x", model="claude-haiku-4-5",
                      usage=Usage(input_tokens=900_000, output_tokens=0))
    governor.check(0.05)  # 0.9 + 0.05 <= 1.0
    with pytest.raises(BudgetExceeded):
        governor.check(0.2)  # 0.9 + 0.2 > 1.0


def test_tier_models_settings_override(tmp_path):
    s = Settings(db_path=tmp_path / "x.db", model_fast="my-fast-model")
    models = tier_models(s)
    assert models[ModelTier.FAST] == "my-fast-model"
    assert models[ModelTier.BALANCED] == "claude-sonnet-4-6"
    assert models[ModelTier.DEEP] == "claude-opus-4-8"
    assert s.daily_llm_budget_usd == 2.0  # NEW setting default


# --- endpoints --------------------------------------------------------------------


def test_spend_endpoint_math(env):
    client, container = env
    spend.record_call(container.db, purpose="enrich_t1",
                      model="claude-haiku-4-5",
                      usage=Usage(input_tokens=2200, output_tokens=300))
    body = client.get("/api/spend", params={"days": 7}).json()
    assert body["daily_cap_usd"] == 2.0
    assert body["today_spent_usd"] == pytest.approx(0.0037)
    assert len(body["days"]) == 7
    assert body["days"][-1]["calls"] == 1
    assert body["days"][-1]["input_tokens"] == 2200
    assert body["days"][-1]["output_tokens"] == 300
    assert body["days"][-1]["cost_usd"] == pytest.approx(0.0037)
    assert body["days"][0] == {"day": body["days"][0]["day"], "calls": 0,
                               "input_tokens": 0, "output_tokens": 0,
                               "cost_usd": 0.0}


def test_enrichment_sweep_endpoint_runs_job(env):
    client, container = env
    # no provider -> 503 with a clear reason
    container.enrichment.provider = None
    resp = client.post("/api/enrichment/sweep", json={"mode": "sync"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]

    # inject the mock provider and ingest one eligible doc
    from test_enrichment import t1_result
    container.enrichment.provider = MockProvider(respond=t1_result())
    created = client.post("/api/documents", json={
        "text": "The Reserve Bank of India raised the repo rate by 25 basis"
                " points to 6.75 percent on Friday."})
    doc_id = created.json()["id"]

    resp = client.post("/api/enrichment/sweep",
                       json={"mode": "sync", "limit": 10})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert isinstance(job_id, int)

    # the job runs on the app's event loop; poll its row via the DB
    for _ in range(200):
        row = container.db.execute(
            "SELECT status, error FROM job WHERE id=?", (job_id,)).fetchone()
        if row["status"] in ("done", "failed"):
            break
        import time
        time.sleep(0.01)
    assert row["status"] == "done", row["error"]
    assert container.db.execute(
        "SELECT enrichment_status FROM document WHERE id=?",
        (doc_id,)).fetchone()[0] == "done"

    # invalid mode rejected
    assert client.post("/api/enrichment/sweep",
                       json={"mode": "nope"}).status_code == 422
