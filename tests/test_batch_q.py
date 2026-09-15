"""Batch Q: the plan of the day. Without the engine the fixed shares apply and the tick runs as before; with it the
treasury is allocated by a verified solve, the chat reserve is never reduced, a short day names its shortfall, the
tick skips what the plan leaves unfunded, vendor stress is fitted or refused, and the proctor reports its precision."""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import jobs, proctor, treasury_plan, vault  # noqa: E402
from orchestrator.quota_registry import get_quota_ledger  # noqa: E402
from orchestrator.society import academy, cycles, leisure, store, templates, tick  # noqa: E402
from orchestrator.treasury_plan import Activity, DayPlan, PlanLine  # noqa: E402

ENGINE = treasury_plan.available()
ACTIVITIES = [
    Activity("chat", "Chat reserve", 120_000, 30.0, min_scale=1.0, lead_days=1.0),
    Activity("company:1", "AVS studio", 300_000, 25.0, min_scale=0.3, lead_days=0.5, prerequisite="chat", company_id=1),
    Activity("academy", "Academy", 250_000, 18.0, min_scale=0.3),
    Activity("missions", "2 queued mission(s)", 150_000, 15.0, min_scale=0.2, lead_days=1.0),
    Activity("leisure", "Leisure", 60_000, 5.0, min_scale=0.2),
]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    vault.initialize_database()
    return "scope-q"


def test_plan_settings_round_trip_and_activities_come_from_the_society(db):
    config = treasury_plan.save_config(db, 0.6, 0.2, {"academy": 20})
    assert config["goal_fraction"] == 0.6 and config["chat_reserve_fraction"] == 0.2 and treasury_plan.config_for(db)["values"]["academy"] == 20.0
    templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 10)
    activities, budget = treasury_plan.build_activities(db, get_quota_ledger())
    keys = [a.key for a in activities]
    assert keys[0] == "chat" and activities[0].min_scale == 1.0 and any(k.startswith("company:") for k in keys) and "academy" in keys and "leisure" in keys
    assert "missions" not in keys and budget > 0 and all(a.cost_tokens > 0 for a in activities)
    assert activities[0].cost_tokens >= treasury_plan.CHAT_RESERVE_MIN_TOKENS


def test_without_the_engine_the_plan_is_the_fixed_shares_and_says_so(db, monkeypatch):
    monkeypatch.setattr(treasury_plan, "available", lambda: False)
    plan = treasury_plan.plan_day(db, get_quota_ledger(), activities=ACTIVITIES, budget=500_000)
    assert plan.status == "engine-missing" and all(line.scale == 1.0 for line in plan.lines) and "not installed" in plan.notes[0]
    assert plan.scale_for("company:1") == 1.0 and plan.scale_for("unknown") == 1.0
    stored = treasury_plan.latest(db)
    assert stored and stored["status"] == "engine-missing" and json.loads(plan.to_json())["lines"][0]["key"] == "chat"
    assert treasury_plan.vendor_stress(db) == (1.0, []) and treasury_plan.tail_risk(db, plan) == {}


@pytest.mark.skipif(not ENGINE, reason="scrcae not installed (requirements-supply.txt, Python 3.12+)")
def test_engine_plan_buys_the_most_value_meets_the_goal_and_pins_the_chat_reserve(db):
    plan = treasury_plan.plan_day(db, get_quota_ledger(), goal_points=55.0, activities=ACTIVITIES, budget=500_000, with_stress=False)
    assert plan.status == "optimal" and plan.value_points >= 55.0 - 1e-6 and plan.spend_tokens <= 500_000 and plan.ms < 5_000
    assert plan.scale_for("chat") == 1.0 and plan.shortfall_tokens == 0 and plan.diagnosis == ""
    again = treasury_plan.plan_day(db, get_quota_ledger(), goal_points=55.0, activities=ACTIVITIES, budget=500_000, with_stress=False, with_tail=False)
    assert again.input_hash == plan.input_hash and again.output_hash == plan.output_hash and again.spend_tokens == plan.spend_tokens
    assert plan.saturation_budget > 0 and 0.0 < plan.recommended_share <= 1.0 and plan.engine
    assert plan.tail and plan.tail["p90_points"] <= plan.tail["p50_points"] <= plan.tail["deterministic_points"] + 1.0 and plan.tail["sigma"] >= treasury_plan.SIGMA_FLOOR
    # The engine's own re-check found nothing to complain about.
    assert not any("violation" in note for note in plan.notes)


@pytest.mark.skipif(not ENGINE, reason="scrcae not installed")
def test_a_short_day_names_the_shortfall_and_still_funds_the_chat_first(db):
    plan = treasury_plan.plan_day(db, get_quota_ledger(), goal_points=80.0, activities=ACTIVITIES, budget=200_000, with_stress=False, with_tail=False)
    assert plan.status == "short" and plan.shortfall_tokens > 0 and "budget" in plan.diagnosis.lower()
    assert plan.scale_for("chat") == 1.0 and plan.spend_tokens <= 200_000 and plan.value_points > 30.0
    unfunded = [line.key for line in plan.lines if line.scale == 0.0]
    assert unfunded  # something was left out rather than everything deferred
    beyond = treasury_plan.plan_day(db, get_quota_ledger(), goal_points=150.0, activities=ACTIVITIES, budget=2_000_000, with_stress=False, with_tail=False)
    assert beyond.status == "ceiling" and beyond.diagnosis and beyond.scale_for("chat") == 1.0


@pytest.mark.skipif(not ENGINE, reason="scrcae not installed")
def test_vendor_stress_is_fitted_from_the_route_log_or_refused(db):
    now = time.time()
    # 72 hourly bins for groq: failures rise with load above the anchor; gemini has too few bins to fit.
    for hour in range(72):
        calls = 2 + (hour * 7) % 9
        for i in range(calls):
            failed = (hour % 3 == 0 and calls >= 8 and i < calls // 2)
            vault.record_route(db, "normal_chat", "chat", "failed" if failed else "groq/m", "normal", 100, "", "groq window" if failed else "r")
            with vault._open_database() as connection:  # spread the rows over the last 72 hours
                connection.execute("UPDATE route_log SET timestamp = ? WHERE id = (SELECT MAX(id) FROM route_log)", (now - (72 - hour) * 3600.0 + i,))
    vault.record_route(db, "normal_chat", "chat", "google_ai_studio/m", "normal", 100, "", "r")
    stress, rows = treasury_plan.vendor_stress(db, now=now)
    by_vendor = {r["vendor"]: r for r in rows}
    assert 0.5 <= stress <= 1.0 and by_vendor["groq"]["bins"] >= treasury_plan.STRESS_MIN_BINS and by_vendor["groq"]["quality"] in ("usable", "no relationship", "wrong sign")
    assert by_vendor["google_ai_studio"]["quality"] == "too few bins" and by_vendor["google_ai_studio"]["multiplier"] == 1.0


def test_the_tick_runs_activities_at_the_planned_size_and_skips_what_the_plan_leaves_out(db, monkeypatch):
    templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 10)
    company_id = int(store.companies_for(db)[0]["id"])
    monkeypatch.setattr(leisure, "run_leisure", lambda *a, **k: [])
    monkeypatch.setattr(academy, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    plan = DayPlan(db, "2026-09-15", 400_000, 60.0, "short", [
        PlanLine("chat", "Chat reserve", 1.0, 60_000, 30.0), PlanLine(f"company:{company_id}", "AVS studio", 0.0, 0, 0.0),
        PlanLine("academy", "Academy", 0.5, 20_000, 9.0), PlanLine("leisure", "Leisure", 0.25, 3_000, 1.25),
    ], spend_tokens=83_000, value_points=40.25, full_value_points=78.0, shortfall_tokens=120_000, diagnosis="short by 120,000")
    monkeypatch.setattr(treasury_plan, "plan_day", lambda scope, ledger, **kwargs: plan)
    job_id = tick.start_tick(db, {"GEMINI_API_KEY": "AIza-fake"}, interval_s=600)
    jobs.run_job(vault.claim_job("t", (tick.KIND_TICK,)))
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["status"] == "done" and view["result"]["skipped_by_plan"] == ["AVS Studio"] and "short by 120,000" in view["result"]["plan"]
    assert not vault.list_jobs(db, ("queued",), kind=cycles.KIND_COMPANY)
    queued = vault.list_jobs(db, ("queued",), kind=academy.KIND_ACADEMY)
    assert queued and vault.job_view(queued[0])["payload"]["max_tokens"] == int(academy.DEFAULT_MAX_TOKENS * 0.5)
    row = store.rows("cycles", db, "kind = 'tick'")[-1]
    log = json.loads(row["log"]) if isinstance(row["log"], str) else row["log"]
    assert log[0]["skipped_by_plan"] == ["AVS Studio"] and "plan" in log[0]
    leisure_row = store.rows("cycles", db, "kind = 'leisure'")[-1]
    assert leisure_row["tokens_planned"] <= int(tick.LEISURE_MAX_TOKENS * 0.25)
    # A planner that raises never stops the scheduler: every scale is 1.0.
    monkeypatch.setattr(treasury_plan, "plan_day", lambda scope, ledger, **kwargs: (_ for _ in ()).throw(RuntimeError("no planner")))
    fallback = tick.day_plan(db, get_quota_ledger(), time.time())
    assert fallback.status == "error" and fallback.scale_for("academy") == 1.0 and "no planner" in fallback.notes[0]


def test_the_proctor_reports_its_own_precision():
    assert proctor.proportion_standard_error(0.5, 100) == pytest.approx(0.05) and proctor.proportion_standard_error(0.0, 2048) == 0.0 and proctor.proportion_standard_error(0.3, 0) == 0.0
    assert proctor.percentile_standard_error([1.0] * 50, 0.5) is None  # too few
    assert proctor.percentile_standard_error([7.0] * 400, 0.5) == 0.0  # flat neighbourhood
    spread = [i / 10.0 for i in range(1000)]
    se = proctor.percentile_standard_error(spread, 0.5)
    assert se is not None and 0.0 < se < 5.0
    hot = proctor.forecast_budget("gemini", 100_000, 90_000, 6.0, paths=256, seed=1)
    assert hot.p50_cap_hour_se is not None and hot.p50_cap_hour_se >= 0.0 and hot.p_cap_today_se == 0.0 and "± h" in hot.row()
    report = proctor.RoutingReport("chat", 1_000, "groq", 2048, (1.0,), fragility=0.1)
    assert report.fragility_se == pytest.approx((0.1 * 0.9 / 2048) ** 0.5) and "±" in report.summary()
