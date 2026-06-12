"""Phase D — budgets, limits, admin API, observability.

TenantGovernor math (member default cap, per-user override, app_setting
precedence, the GLOBAL backstop trumping everything), llm_call/job user
attribution end-to-end through a real analysis job, the interactive
backpressure 429s, per-user rate limits, the /api/admin surface
(users/settings/spend + guards), the /api/spend my-spend split, deep
health redaction, and admin-only metrics. NO network; MockProvider only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import login
from dbutil import q1, qall, qv
from test_analysis_api import make_provider, run_analysis, seed_corpus

from connect.api.main import create_app
from connect.auth import sessions as auth_sessions
from connect.auth.sessions import SESSION_COOKIE
from connect.llm import spend
from connect.llm.spend import (
    BudgetExceeded,
    CURRENT_USER_ID,
    GENERAL_SCOPE,
    INVESTIGATION_SCOPE,
    TenantGovernor,
)
from connect.storage import app_settings as app_settings_dao
from connect.storage import users as user_dao
from connect.storage.pg import utc_now


# --- environment ---------------------------------------------------------------

@pytest.fixture()
def env(settings):
    """One app; the dev-login admin signed in. ``make_member`` adds a
    second (member) identity with a real session row."""
    app = create_app(settings)
    with TestClient(app) as client:
        me = login(client)
        sid = client.cookies.get(SESSION_COOKIE)
        yield {"client": client, "container": app.state.container,
               "me": me, "sid_admin": sid}


async def make_member(db, settings, email="member@test.local"):
    row = await user_dao.insert(db, email=email, role="member")
    sid = await auth_sessions.create_session(db, row["id"], settings)
    return int(row["id"]), sid


def act_as(env, sid: str) -> TestClient:
    client = env["client"]
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, sid)
    return client


async def ledger(conn, purpose: str, cost: float,
                 user_id: int | None = None,
                 day_offset: int = 0) -> None:
    ts = utc_now()
    if day_offset:
        when = datetime.now(timezone.utc) - timedelta(days=day_offset)
        ts = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    await conn.execute(
        "INSERT INTO llm_call (purpose, model, cost_estimate, created_at,"
        " user_id) VALUES (%s, 'claude-haiku-4-5', %s, %s, %s)",
        (purpose, cost, ts, user_id))


async def insert_job(conn, kind="analysis", status="queued",
                     owner_id=None) -> int:
    cur = await conn.execute(
        "INSERT INTO job (kind, status, created_at, owner_id)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (kind, status, utc_now(), owner_id))
    return int((await cur.fetchone())["id"])


# ==============================================================================
# TenantGovernor math
# ==============================================================================

async def test_member_default_cap_blocks(env, db, settings):
    container = env["container"]
    member, _sid = await make_member(db, settings)
    gov: TenantGovernor = container.governor
    await ledger(db, "analysis", 0.45, user_id=member)
    await gov.check(0.01, user_id=member)  # 0.46 <= $0.50: fine
    with pytest.raises(BudgetExceeded) as e:
        await gov.check(0.10, user_id=member)  # 0.55 > $0.50
    msg = str(e.value)
    assert "your daily general budget" in msg
    assert "$0.50" in msg and "remaining" in msg  # friendly + remaining


async def test_member_scopes_are_separate_envelopes(env, db, settings):
    container = env["container"]
    member, _sid = await make_member(db, settings)
    # $0.49 of general spend leaves the $2 investigation ceiling untouched
    await ledger(db, "analysis", 0.49, user_id=member)
    await container.investigation_governor.check(1.9, user_id=member)
    # and investigation spend never charges the general ceiling
    await ledger(db, "investigation", 1.9, user_id=member)
    await container.governor.check(0.0, user_id=member)


async def test_admin_keeps_envelope_ceiling(env, db):
    container = env["container"]
    admin = env["me"]["id"]
    await ledger(db, "analysis", 1.5, user_id=admin)  # way past $0.50
    await container.governor.check(0.10, user_id=admin)  # $2 admin ceiling
    with pytest.raises(BudgetExceeded):
        await container.governor.check(0.6, user_id=admin)  # 2.1 > $2


async def test_per_user_override_trumps_defaults(env, db, settings):
    container = env["container"]
    member, _sid = await make_member(db, settings)
    await db.execute(
        "UPDATE app_user SET daily_budget_usd = 5.0 WHERE id = %s",
        (member,))
    await ledger(db, "analysis", 1.0, user_id=member)
    await container.governor.check(0.5, user_id=member)  # under override
    await db.execute(
        "UPDATE app_user SET daily_budget_usd = 0.25 WHERE id = %s",
        (member,))
    with pytest.raises(BudgetExceeded):
        await container.governor.check(0.0, user_id=member)


async def test_app_setting_member_default_wins_over_env(env, db, settings):
    container = env["container"]
    member, _sid = await make_member(db, settings)
    await app_settings_dao.set_value(db, "member_daily_budget_usd", "1.5")
    await ledger(db, "analysis", 1.0, user_id=member)
    await container.governor.check(0.1, user_id=member)  # 1.1 <= 1.5
    await app_settings_dao.set_value(db, "member_daily_budget_usd", "0.5")
    with pytest.raises(BudgetExceeded):
        await container.governor.check(0.1, user_id=member)


async def test_global_backstop_trumps_everything(env, db, settings):
    """A member with a huge override and a free purpose envelope is still
    refused once the DEPLOYMENT's day is spent (locked $10 backstop)."""
    container = env["container"]
    member, _sid = await make_member(db, settings)
    await db.execute(
        "UPDATE app_user SET daily_budget_usd = 100.0 WHERE id = %s",
        (member,))
    # system investigation spend: invisible to the general envelope,
    # counted by the global backstop
    await ledger(db, "investigation", 9.95, user_id=None)
    with pytest.raises(BudgetExceeded) as e:
        await container.governor.check(0.10, user_id=member)
    assert "deployment's daily LLM budget" in str(e.value)


async def test_system_scope_skips_user_ceiling(env, db):
    """user_id=None (system jobs) is gated by the global + purpose
    envelopes only — there is no user ceiling to trip."""
    container = env["container"]
    await ledger(db, "enrich_t1", 1.5, user_id=None)  # > any member cap
    await container.governor.check(0.1, user_id=None)  # $2 envelope: fine
    with pytest.raises(BudgetExceeded):
        await container.governor.check(0.6, user_id=None)  # envelope


async def test_ambient_user_drives_check_and_attribution(env, db,
                                                         settings):
    container = env["container"]
    member, _sid = await make_member(db, settings)
    await ledger(db, "analysis", 0.45, user_id=member)
    token = CURRENT_USER_ID.set(member)
    try:
        from connect.llm.provider import Usage
        await spend.record_call(db, purpose="analysis",
                                model="claude-haiku-4-5",
                                usage=Usage(input_tokens=10,
                                            output_tokens=10))
        assert await qv(db, "SELECT user_id FROM llm_call"
                            " ORDER BY id DESC LIMIT 1") == member
        with pytest.raises(BudgetExceeded) as e:
            await container.governor.check(0.10)  # no explicit user_id
        assert "your daily general budget" in str(e.value)
    finally:
        CURRENT_USER_ID.reset(token)
    # outside the job context the same call is system scope again
    await container.governor.check(0.10)


# ==============================================================================
# attribution end-to-end: a real analysis job charges its owner
# ==============================================================================

async def test_analysis_job_charges_owner(env, db):
    client, container = env["client"], env["container"]
    me = env["me"]["id"]
    await seed_corpus(db)
    analysis_id, job_id = await run_analysis(client, container, db)
    assert await qv(db, "SELECT owner_id FROM job WHERE id=%s",
                    job_id) == me
    rows = await qall(db, "SELECT user_id, purpose FROM llm_call")
    assert rows, "the analysis must ledger calls"
    assert all(r["user_id"] == me for r in rows), rows


# ==============================================================================
# interactive backpressure (429)
# ==============================================================================

async def test_user_max_interactive_429(env, db):
    client, container = env["client"], env["container"]
    me = env["me"]["id"]
    container.analysis.provider = make_provider()
    for _ in range(2):  # CONNECT_USER_MAX_INTERACTIVE default
        await insert_job(db, kind="analysis", status="queued", owner_id=me)
    res = client.post("/api/analyses",
                      json={"input_text": "RBI raised the repo rate"})
    assert res.status_code == 429, res.text
    assert res.headers.get("retry-after") == "60"
    assert "in flight" in res.json()["detail"]


async def test_interactive_queue_limit_429(env, db, settings):
    """Global queue depth gates EVERY user, not just the busy one."""
    crowded = settings.model_copy(
        update={"interactive_queue_limit": 1})
    app = create_app(crowded)
    with TestClient(app) as client:
        login(client)
        container = app.state.container
        container.investigations.provider = make_provider()
        other, _sid = await make_member(db, settings,
                                        email="busy@test.local")
        await insert_job(db, kind="investigation", status="queued",
                         owner_id=other)
        res = client.post("/api/investigations", json={"topic": "why"})
        assert res.status_code == 429, res.text
        assert "busy" in res.json()["detail"]
        assert res.headers.get("retry-after") == "60"


async def test_budget_429_on_create(env, db):
    """Pre-accept TenantGovernor check: an exhausted ceiling answers 429
    with the friendly message instead of accepting a doomed job."""
    client, container = env["client"], env["container"]
    me = env["me"]["id"]
    container.analysis.provider = make_provider()
    await ledger(db, "analysis", 2.0, user_id=me)  # admin ceiling spent
    res = client.post("/api/analyses",
                      json={"input_text": "RBI raised the repo rate"})
    assert res.status_code == 429, res.text
    assert "budget" in res.json()["detail"]


# ==============================================================================
# per-user rate limits
# ==============================================================================

def test_rate_limit_reads_and_sse_exemption(settings):
    limited = settings.model_copy(
        update={"rate_limit_enabled": True,
                "rate_limit_read_per_minute": 3})
    app = create_app(limited)
    with TestClient(app) as client:
        login(client)
        for _ in range(3):
            assert client.get("/api/sources").status_code == 200
        res = client.get("/api/sources")
        assert res.status_code == 429
        assert "rate limit" in res.json()["detail"]
        assert res.headers.get("retry-after") == "60"
        # SSE endpoints are exempt — never rate-limited (404: no such
        # analysis, but NOT 429)
        res = client.get("/api/analyses/999999/events")
        assert res.status_code == 404


def test_rate_limit_disabled_by_default_in_tests(client):
    for _ in range(5):
        assert client.get("/api/sources").status_code == 200


# ==============================================================================
# admin API: guards, users, settings, spend
# ==============================================================================

ADMIN_SURFACES = ["/api/admin/users", "/api/admin/invites",
                  "/api/admin/settings", "/api/admin/spend",
                  "/api/metrics"]


async def test_admin_guards(env, db, settings):
    _member, sid = await make_member(db, settings)
    client = act_as(env, sid)
    for path in ADMIN_SURFACES:
        assert client.get(path).status_code == 403, path
    client.cookies.clear()  # anonymous
    for path in ADMIN_SURFACES:
        assert client.get(path).status_code == 401, path


async def test_admin_users_list_patch_and_override_clear(env, db,
                                                         settings):
    client = env["client"]
    member, _sid = await make_member(db, settings)
    users = client.get("/api/admin/users").json()
    assert {u["email"] for u in users} >= {"dev@test.local",
                                           "member@test.local"}

    res = client.patch(f"/api/admin/users/{member}",
                       json={"daily_budget_usd": 1.25})
    assert res.status_code == 200, res.text
    assert res.json()["daily_budget_usd"] == 1.25
    gov: TenantGovernor = env["container"].governor
    assert await gov.user_cap(db, member) == 1.25

    # explicit null clears the override back to the member default
    res = client.patch(f"/api/admin/users/{member}",
                       json={"daily_budget_usd": None})
    assert res.status_code == 200
    assert res.json()["daily_budget_usd"] is None
    assert await gov.user_cap(db, member) == 0.50

    assert client.patch("/api/admin/users/999999",
                        json={"disabled": True}).status_code == 404
    assert client.patch(f"/api/admin/users/{member}",
                        json={}).status_code == 422
    assert client.patch(f"/api/admin/users/{member}",
                        json={"daily_budget_usd": -1}).status_code == 422


async def test_admin_disable_revokes_sessions(env, db, settings):
    admin_sid = env["sid_admin"]
    member, member_sid = await make_member(db, settings)
    client = act_as(env, member_sid)
    assert client.get("/api/sources").status_code == 200
    client = act_as(env, admin_sid)
    res = client.patch(f"/api/admin/users/{member}",
                       json={"disabled": True})
    assert res.status_code == 200 and res.json()["disabled"] is True
    client = act_as(env, member_sid)
    assert client.get("/api/sources").status_code == 401  # revoked NOW
    act_as(env, admin_sid)


async def test_admin_cannot_demote_or_disable_self(env):
    client, me = env["client"], env["me"]["id"]
    assert client.patch(f"/api/admin/users/{me}",
                        json={"disabled": True}).status_code == 409
    assert client.patch(f"/api/admin/users/{me}",
                        json={"role": "member"}).status_code == 409


async def test_admin_promote_member(env, db, settings):
    client = env["client"]
    member, member_sid = await make_member(db, settings)
    res = client.patch(f"/api/admin/users/{member}",
                       json={"role": "admin"})
    assert res.status_code == 200 and res.json()["role"] == "admin"
    client = act_as(env, member_sid)
    assert client.get("/api/admin/users").status_code == 200
    act_as(env, env["sid_admin"])


async def test_admin_settings_roundtrip(env, db):
    client = env["client"]
    body = client.get("/api/admin/settings").json()
    assert body["global_daily_budget_usd"] == 10.0
    assert body["member_daily_budget_usd"] == 0.5

    res = client.patch("/api/admin/settings",
                       json={"global_daily_budget_usd": 25,
                             "member_daily_budget_usd": 1.0})
    assert res.status_code == 200, res.text
    assert res.json()["global_daily_budget_usd"] == 25.0
    assert await app_settings_dao.get(
        db, "global_daily_budget_usd") == "25.0"
    # the governors read app_setting per check — live everywhere, no
    # restart: the member ceiling is now $1.00
    gov: TenantGovernor = env["container"].governor
    member, _sid = await make_member(db, env["container"].settings)
    assert await gov.user_cap(db, member) == 1.0

    assert client.patch("/api/admin/settings",
                        json={}).status_code == 422
    assert client.patch("/api/admin/settings",
                        json={"global_daily_budget_usd": -5}
                        ).status_code == 422


async def test_admin_spend_breakdown(env, db, settings):
    client, me = env["client"], env["me"]["id"]
    member, _sid = await make_member(db, settings)
    await ledger(db, "analysis", 0.30, user_id=me)
    await ledger(db, "investigation", 0.50, user_id=member)
    await ledger(db, "enrich_t1", 0.20, user_id=None)        # system
    await ledger(db, "analysis", 0.10, user_id=member, day_offset=1)

    body = client.get("/api/admin/spend?days=2").json()
    assert body["global_cap_usd"] == 10.0
    assert body["global_today_usd"] == pytest.approx(1.0)
    assert len(body["days"]) == 2
    by_id = {u["user_id"]: u for u in body["users"]}
    assert by_id[me]["today_usd"] == pytest.approx(0.30)
    assert by_id[me]["email"] == "dev@test.local"
    assert by_id[me]["cap_usd"] == 2.0                # admin envelope
    assert by_id[member]["today_usd"] == pytest.approx(0.50)
    assert by_id[member]["cap_usd"] == 0.5            # member default
    assert by_id[member]["investigation_cap_usd"] == 2.0
    assert by_id[member]["days"][0]["cost_usd"] == pytest.approx(0.10)
    assert by_id[None]["today_usd"] == pytest.approx(0.20)   # system row
    assert by_id[None]["cap_usd"] is None


# ==============================================================================
# /api/spend — my spend + global context
# ==============================================================================

async def test_spend_mine_and_global(env, db, settings):
    client, me = env["client"], env["me"]["id"]
    member, _sid = await make_member(db, settings)
    await ledger(db, "analysis", 0.20, user_id=me)
    await ledger(db, "analysis", 0.30, user_id=member)

    body = client.get("/api/spend").json()
    assert body["today_spent_usd"] == pytest.approx(0.50)  # legacy field
    assert body["mine"]["today_usd"] == pytest.approx(0.20)
    assert body["mine"]["cap_usd"] == 2.0
    assert body["mine"]["investigation_cap_usd"] == 10.0
    assert body["global"]["today_usd"] == pytest.approx(0.50)
    assert body["global"]["cap_usd"] == 10.0  # the global backstop
    assert len(body["mine"]["days"]) == 7


# ==============================================================================
# deep health + metrics
# ==============================================================================

async def test_health_deep_state_and_redaction(env, db):
    client = env["client"]
    await insert_job(db, kind="analysis", status="queued",
                     owner_id=env["me"]["id"])
    await db.execute(
        "INSERT INTO beat_run (task, last_run_at) VALUES (%s, %s)",
        ("enrich_t1_batch", utc_now()))
    await ledger(db, "analysis", 0.25, user_id=env["me"]["id"])

    res = client.get("/api/health?deep=1")
    assert res.status_code == 200
    body = res.json()
    deep = body["deep"]
    assert deep["pg_ok"] is True
    assert {"kind": "analysis", "status": "queued", "count": 1} \
        in deep["queue"]
    assert deep["queue_oldest_seconds"] >= 0.0
    assert deep["beat_runs"]["enrich_t1_batch"]
    assert deep["blob_dir_writable"] is True
    assert deep["budget"]["global_cap_usd"] == 10.0
    assert deep["budget"]["today_usd"] == pytest.approx(0.25)
    assert deep["budget"]["general_today_usd"] == pytest.approx(0.25)
    assert deep["budget"]["investigation_today_usd"] == 0.0
    assert isinstance(deep["sources"], list)

    # NO secrets: password redacted, no keys, no session secret
    text = res.text
    assert ":connect@" not in text          # the test DSN's password
    assert "***" in body["db_path"]
    assert "ANTHROPIC" not in text and "test-secret" not in text

    # shallow health stays the cheap v0.1 shape (no deep key at all)
    shallow = client.get("/api/health").json()
    assert "deep" not in shallow


async def test_health_deep_is_open(anon_client):
    res = anon_client.get("/api/health?deep=1")
    assert res.status_code == 200
    assert res.json()["deep"]["pg_ok"] is True


async def test_metrics_admin_only_and_content(env, db, settings):
    client = env["client"]
    await insert_job(db, kind="analysis", status="queued",
                     owner_id=env["me"]["id"])
    await ledger(db, "investigation", 0.40, user_id=env["me"]["id"])
    res = client.get("/api/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    text = res.text
    assert 'connect_jobs{kind="analysis",status="queued"} 1' in text
    assert 'connect_llm_spend_usd_today{scope="investigation"} 0.4' in text
    assert "connect_users_total" in text
    assert "connect_sessions_active" in text
    assert "connect_job_queue_oldest_seconds" in text

    _member, sid = await make_member(db, settings)
    assert act_as(env, sid).get("/api/metrics").status_code == 403
