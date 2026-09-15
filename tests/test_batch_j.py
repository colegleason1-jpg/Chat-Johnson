"""Batch J: the Monte Carlo proctor (routing fragility, bursty budget forecast) and the tick's deferral on the forecast."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import jobs, pinkwave, proctor, vault  # noqa: E402
from orchestrator.quota_registry import get_quota_ledger  # noqa: E402
from orchestrator.router import CORTEX_ENDPOINTS, ProviderError  # noqa: E402
from orchestrator.society import academy, cycles, leisure, store, templates, tick  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    pinkwave.deactivate()
    proctor.reset_for_tests()
    yield "scope-j"
    pinkwave.deactivate()
    proctor.reset_for_tests()


# ----------------------------------------------------------------------------- routing fragility

def test_fragility_report_is_reproducible_bounded_and_never_crowns_a_blocked_endpoint(db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    pinkwave.activate(db, pinkwave.ChaosSettings(gain=1.0))  # the proctor suspends the live wave while it simulates
    report = proctor.simulate_routing("chat", 1_500, paths=2_048, seed=5)
    again = proctor.simulate_routing("chat", 1_500, paths=2_048, seed=5)
    assert report.win_rates == again.win_rates and report.deterministic == "groq" and report.ms < 5_000
    assert set(report.win_rates) == {"x1", "x2", "x4", "x8"} and all(abs(sum(r.values()) - 1.0) < 1e-9 for r in report.win_rates.values())
    assert report.fragility == 0.0 and 0.0 <= report.fragility_amplified <= 1.0  # the current table is robust at production strength
    assert report.entropy_bits["x1"] == 0.0 and report.entropy_bits["x8"] >= report.entropy_bits["x1"] and report.max_entropy_bits == 1.0
    assert "huggingface" in report.blocked and all(r.get("huggingface", 0.0) == 0.0 for r in report.win_rates.values())
    assert all(name not in report.blocked for name in report.outliers) and "groq" in report.summary() and "bits" in report.summary()
    rows = {r["endpoint"]: r for r in report.rows()}
    assert rows["groq"]["deterministic"] and rows["huggingface"]["blocked"]
    exhausted = proctor.simulate_routing("chat", 1_500, paths=64, seed=5, current_usage={"groq": {"rpm_used": 30, "tpm_used": 0}})
    assert exhausted.deterministic == "google_ai_studio" and all(r.get("groq", 0.0) == 0.0 for r in exhausted.win_rates.values())
    assert pinkwave.current() is not None  # the live wave is restored after the simulation
    with pytest.raises(ProviderError):
        proctor.simulate_routing("chat", 1_500, paths=8, excluded=list(CORTEX_ENDPOINTS))


def test_amplified_chaos_draws_entropy_out_of_a_near_tie(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    from orchestrator import router

    # Make chat a near-tie: pull groq's speed edge down so the two utilities sit within the production jitter band.
    groq = router.CORTEX_ENDPOINTS["groq"]
    original = groq.speed_score
    object.__setattr__(groq, "speed_score", 0.72)  # chat utilities: groq 0.771 vs gemini 0.762, inside the production jitter band
    try:
        report = proctor.simulate_routing("chat", 1_500, paths=1_024, seed=9)
        assert 0.0 < report.fragility < 1.0 and report.entropy_bits["x1"] > 0.0
        assert report.entropy_bits["x8"] >= report.entropy_bits["x1"] * 0.5  # amplification keeps the tie contested
    finally:
        object.__setattr__(groq, "speed_score", original)


def test_cached_fragility_reruns_at_most_once_per_ttl(db, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    calls = []
    real = proctor.simulate_routing

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(proctor, "simulate_routing", counting)
    first = proctor.cached_fragility("chat", 1_200)
    second = proctor.cached_fragility("chat", 1_900)  # same thousand-token bucket
    assert first == second and 0.0 <= first <= 1.0 and len(calls) == 1
    proctor.cached_fragility("reasoning", 1_200)
    assert len(calls) == 2
    monkeypatch.setattr(proctor, "simulate_routing", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    proctor.reset_for_tests()
    assert proctor.cached_fragility("chat", 1_200) is None


# ----------------------------------------------------------------------------- budget forecast

def test_budget_forecast_caps_early_when_the_rate_is_high_and_never_when_it_is_low():
    hot = proctor.forecast_budget("gemini", 100_000, 90_000, 6.0, paths=64, seed=1)
    assert hot.p_cap_today == 1.0 and hot.p50_cap_hour is not None and 6.0 < hot.p50_cap_hour < 12.0 and hot.p10_cap_hour <= hot.p50_cap_hour
    assert hot.row()["remaining"] == 10_000 and "caps around" in hot.summary()
    cold = proctor.forecast_budget("gemini", 100_000, 1_000, 12.0, paths=64, seed=1)
    assert cold.p_cap_today == 0.0 and cold.p50_cap_hour is None and "unlikely" in cold.summary()
    exact = proctor.forecast_budget("groq", 100_000, 90_000, 6.0, paths=8, sigma=0.0)
    assert exact.p_cap_today == 1.0 and abs(exact.p50_cap_hour - (6.0 + 0.75)) < 1e-9  # 15k/h, 10k left, quarter-hour steps
    assert proctor.forecast_budget("local", 0, 5, 3.0).summary() == "local: uncapped"
    capped = proctor.forecast_budget("hf", 1_000, 1_000, 9.0)
    assert capped.p_cap_today == 1.0 and capped.p50_cap_hour == 9.0
    assert proctor.forecast_budget("x", 100, 50, 6.0, seed=3, paths=32).p_cap_today == proctor.forecast_budget("x", 100, 50, 6.0, seed=3, paths=32).p_cap_today


def test_forecast_vendors_reads_the_shared_counters_and_should_defer_needs_every_vendor_short(db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    ledger = get_quota_ledger()
    ledger.set_daily_limit("gemini", 10_000)
    ledger.record("gemini", 9_900, count_request=False)
    reports = {r.vendor: r for r in proctor.forecast_vendors(ledger, ["gemini", "groq", "gemini"], now=time.time())}
    assert set(reports) == {"gemini", "groq"} and reports["gemini"].used >= 9_900 and reports["gemini"].cap == 10_000
    short = proctor.BudgetReport("gemini", 10_000, 9_900, 12.0, 800.0, 8)
    soon = proctor.BudgetReport("groq", 50_000, 20_000, 12.0, 6_000.0, 8, p_cap_today=0.9, p10_cap_hour=12.2, p50_cap_hour=12.5)
    clear = proctor.BudgetReport("hf", 50_000, 1_000, 12.0, 100.0, 8, p_cap_today=0.0)
    uncapped = proctor.BudgetReport("local", 0, 0, 12.0, 0.0, 8)
    assert proctor.should_defer([short, soon], 1_000)[0]
    assert "gemini: 100 tokens left" in proctor.should_defer([short, soon], 1_000)[1]
    assert not proctor.should_defer([short, clear], 1_000)[0] and not proctor.should_defer([short, uncapped], 1_000)[0]
    assert not proctor.should_defer([], 1_000)[0]
    late = proctor.BudgetReport("groq", 50_000, 20_000, 12.0, 6_000.0, 8, p_cap_today=0.9, p50_cap_hour=15.0)
    assert not proctor.should_defer([late], 1_000)[0]  # capping later than the horizon is not a reason to wait now


def test_the_tick_defers_company_and_academy_cycles_on_the_forecast(db, monkeypatch):
    templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 10)
    monkeypatch.setattr(leisure, "run_leisure", lambda *a, **k: [])
    monkeypatch.setattr(academy, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    short = proctor.BudgetReport("gemini", 10_000, 9_990, 12.0, 800.0, 8)
    monkeypatch.setattr(proctor, "forecast_vendors", lambda ledger, vendors, now=None, paths=0: [short])
    job_id = tick.start_tick(db, {"GEMINI_API_KEY": "AIza-fake"}, interval_s=600)
    jobs.run_job(vault.claim_job("t", (tick.KIND_TICK,)))
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["status"] == "done" and view["result"]["queued"] == [] and "deferred by the budget forecast" in view["result"]["deferred"]
    assert not vault.list_jobs(db, ("queued",), kind=cycles.KIND_COMPANY) and not vault.list_jobs(db, ("queued",), kind=academy.KIND_ACADEMY)
    assert vault.list_jobs(db, ("queued",), kind=tick.KIND_TICK)  # the tick itself keeps its chain
    assert any("deferred by the budget forecast" in str(c["log"]) for c in store.rows("cycles", db, "kind = 'tick'"))
    monkeypatch.setattr(proctor, "forecast_vendors", lambda ledger, vendors, now=None, paths=0: (_ for _ in ()).throw(RuntimeError("no forecast")))
    assert tick.budget_deferral(None, 1_000) == ""  # a failing forecast never blocks the scheduler


def test_route_log_keeps_the_fragility_and_the_comparison_averages_it(db):
    vault.record_route(db, "normal_chat", "chat", "groq/m", "normal", 100, "", "r", chaos={"gain": 0.5, "profile": "pink"}, message_id=1, fragility=0.25)
    vault.record_route(db, "normal_chat", "chat", "groq/m", "normal", 100, "", "r", chaos={"gain": 0.5, "profile": "pink"}, message_id=2, fragility=0.75)
    vault.record_route(db, "normal_chat", "chat", "groq/m", "normal", 100, "", "r", message_id=3)
    comparison = {r["setting"]: r for r in vault.chaos_comparison(db)}
    assert comparison["chaos on"]["mean_fragility"] == 0.5 and comparison["chaos off"]["mean_fragility"] is None
    assert ",outcome,fragility" in vault.routes_csv(db).splitlines()[0] and ",0.25" in vault.routes_csv(db)
