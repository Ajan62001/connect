"""S5 — production integrity observability + live-eval gate."""

from __future__ import annotations

from connect.integrity import dashboard, evaluate, observe, thresholds
from connect.integrity.signals import IntegritySignal
from connect.llm.observability import null_tracer


async def _emit(conn, **kw):
    await observe.record(conn, IntegritySignal(**kw))


# -- observe ------------------------------------------------------------------

async def test_record_writes_event(pg_conn):
    await _emit(pg_conn, kind="gate", surface="content", subject_id=7,
                numerator=2, denominator=10, value="flagged")
    cur = await pg_conn.execute(
        "SELECT kind, numerator, denominator, value FROM integrity_event")
    row = await cur.fetchone()
    assert row["kind"] == "gate" and row["numerator"] == 2
    assert row["value"] == "flagged"


async def test_record_gate_emits_gate_and_contested(pg_conn):
    gate = {"verdict": "flagged", "checked": 8,
            "flagged": [{"reason": "not_entailed"}],
            "contested": [{"claim_id": 1, "verdict": "refuted"}]}
    await observe.record_gate(pg_conn, gate, surface="content", subject_id=3)
    cur = await pg_conn.execute(
        "SELECT kind FROM integrity_event ORDER BY kind")
    kinds = [r["kind"] for r in await cur.fetchall()]
    assert kinds == ["contested", "gate"]


async def test_record_never_raises_on_tracer_failure(pg_conn):
    class _BoomTracer:
        def score(self, **_k):
            raise RuntimeError("langfuse down")
    # must swallow the tracer error after writing the row
    await observe.record(pg_conn, IntegritySignal(
        kind="gate", surface="content", numerator=1, denominator=2),
        tracer=_BoomTracer())
    cur = await pg_conn.execute("SELECT count(*) AS n FROM integrity_event")
    assert (await cur.fetchone())["n"] == 1


def test_null_tracer_score_is_noop():
    null_tracer().score(name="x", value=0.5)  # must not raise


# -- dashboard ----------------------------------------------------------------

async def test_dashboard_rates(pg_conn):
    await _emit(pg_conn, kind="gate", surface="content", numerator=1,
                denominator=10, value="flagged")
    await _emit(pg_conn, kind="gate", surface="content", numerator=0,
                denominator=10, value="pass")
    await _emit(pg_conn, kind="verdict", surface="analysis", value="refuted")
    out = await dashboard.integrity_breakdown(pg_conn, days=14)
    gate = next(t for t in out["totals"] if t["kind"] == "gate")
    assert gate["rate"] == 1 / 20            # 1 flagged of 20 checked
    assert out["verdict_distribution"] == {"refuted": 1}


# -- thresholds ---------------------------------------------------------------

def test_floor_breach():
    r = thresholds.evaluate({"gate_flag_rate": 0.9}, baseline=None)
    assert r and r[0]["type"] == "floor"


def test_within_floor_no_alert():
    assert thresholds.evaluate({"gate_flag_rate": 0.1}, baseline=None) == []


def test_regression_vs_baseline():
    r = thresholds.evaluate(
        {"gate_flag_rate": 0.3}, baseline={"gate_flag_rate": 0.05},
        regression_delta=0.15)
    assert any(x["type"] == "regression" for x in r)


# -- the live eval gate -------------------------------------------------------

async def test_run_eval_persists_and_flags_regression(pg_conn):
    # a run of mostly-flagged gate events should breach the flag-rate floor
    await _emit(pg_conn, kind="gate", surface="content", numerator=9,
                denominator=10, value="flagged")
    run = await evaluate.run_eval(pg_conn, days=1, trigger="test")
    assert run["metrics"]["gate_flag_rate"] == 0.9
    assert run["status"] == "regressed"
    cur = await pg_conn.execute(
        "SELECT status, trigger FROM integrity_eval_run WHERE id=%s",
        (run["id"],))
    row = await cur.fetchone()
    assert row["status"] == "regressed" and row["trigger"] == "test"


async def test_run_eval_healthy_when_clean(pg_conn):
    await _emit(pg_conn, kind="gate", surface="content", numerator=0,
                denominator=20, value="pass")
    run = await evaluate.run_eval(pg_conn, days=1, trigger="test")
    assert run["status"] == "ok" and run["regressions"] == []
