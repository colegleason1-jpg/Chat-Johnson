"""Batch L: Cortex 2 as an empirical learner (measured speed, Bayesian quality, pink-wave exploration, dynamics laws)."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import dynamics, learner, pinkwave, vault  # noqa: E402
from orchestrator import router  # noqa: E402
from orchestrator.router import CORTEX_ENDPOINTS, EndpointUsage, RouteDecision, _utility_score, select_milp_endpoint  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    vault.initialize_database()
    pinkwave.deactivate()
    learner.invalidate()
    dynamics.reset_for_tests()
    router.ENDPOINT_TELEMETRY.clear()
    yield "scope-l"
    pinkwave.deactivate()
    learner.invalidate()
    dynamics.reset_for_tests()
    router.ENDPOINT_TELEMETRY.clear()


ZERO = {name: 0.0 for name in CORTEX_ENDPOINTS}


def send(scope, endpoint, ms, task="chat", message_id=None, explored=False, gain=0.5):
    return vault.record_route(scope, "normal_chat", task, f"{endpoint}/model", "normal", ms, "", "r", chaos={"gain": gain, "profile": "pink"}, message_id=message_id, explored=explored)


# ----------------------------------------------------------------------------- step 1 · scoring hooks

def test_speed_is_derived_from_recorded_p50_and_blended_while_observations_are_few(db):
    groq = CORTEX_ENDPOINTS["groq"]
    fresh = learner.Learner(db)
    assert fresh.speed(groq) == (groq.speed_score, 0, None)  # no data: the table value
    for ms in (4_000, 4_500, 5_000):
        send(db, "groq", ms)
    learner.invalidate(db)
    speed, n, p50 = learner.Learner(db).speed(groq)
    measured = learner.speed_from_p50(4_500)
    expected = (3 / 13) * measured + (10 / 13) * groq.speed_score
    assert n == 3 and p50 == 4_500 and abs(speed - expected) < 1e-9 and speed < groq.speed_score
    for _ in range(40):
        send(db, "groq", 5_000)
    learner.invalidate(db)
    speed, n, _ = learner.Learner(db).speed(groq)
    weight = 43 / 53
    assert n == 43 and abs(speed - (weight * learner.speed_from_p50(5_000) + (1 - weight) * groq.speed_score)) < 1e-9  # measured dominates
    assert learner.speed_from_p50(100) == 1.0 and learner.speed_from_p50(9_000) == learner.SPEED_FLOOR
    router.record_telemetry("google_ai_studio", 5.0, True)  # in-memory telemetry is the second source
    speed, n, p50 = learner.Learner(db).speed(CORTEX_ENDPOINTS["google_ai_studio"])
    assert n == 1 and p50 == 5_000 and speed < CORTEX_ENDPOINTS["google_ai_studio"].speed_score


def test_quality_prior_updates_from_verdicts_and_rebuilds_from_the_outcome_log(db):
    send(db, "groq", 300, message_id=1)
    send(db, "groq", 300, message_id=2)
    send(db, "google_ai_studio", 900, message_id=3)
    assert vault.set_route_outcome(db, 1, "up") == 1 and vault.set_route_outcome(db, 2, "locked") == 1 and vault.set_route_outcome(db, 3, "down") == 1
    view = learner.Learner(db)
    mean, std, n = view.quality("groq", "chat")
    assert n == 2 and abs(mean - 4 / 6) < 1e-9 and std > 0
    down_mean, _, down_n = view.quality("google_ai_studio", "chat")
    assert down_n == 1 and abs(down_mean - 2 / 5) < 1e-9
    assert view.quality("huggingface", "chat") == (0.5, pytest.approx((4 / (16 * 5)) ** 0.5), 0)
    vault.quality_priors_reset(db)
    assert learner.Learner(db).quality("groq", "chat")[2] == 0
    assert learner.rebuild_priors(db) == 3 and learner.Learner(db).quality("groq", "chat")[2] == 2
    assert not learner.observe_outcome(db, "failed", "chat", "up") and not learner.observe_outcome(db, "groq", "chat", "meh")


def test_utility_uses_learned_values_and_the_learner_moves_a_close_call_but_never_a_blocked_endpoint(db):
    groq, gemini = CORTEX_ENDPOINTS["groq"], CORTEX_ENDPOINTS["google_ai_studio"]
    base = _utility_score(groq, "chat", 0.0)
    assert _utility_score(groq, "chat", 0.0, quality=1.0) == pytest.approx(base + router.QUALITY_WEIGHT / 2)
    assert _utility_score(groq, "chat", 0.0, quality=0.0) == pytest.approx(base - router.QUALITY_WEIGHT / 2)
    assert _utility_score(groq, "chat", 0.0, speed=0.5) < base and _utility_score(groq, "chat", 0.0, modulation=-0.1) == pytest.approx(base - 0.1)
    # Off: the selector is the classic one. On: 40 slow sends and ten thumbs-down move a chat request off groq.
    assert select_milp_endpoint("chat", 500, entropy_by_endpoint=ZERO).endpoint.name == "groq"
    for i in range(40):
        send(db, "groq", 6_000, message_id=100 + i)
    for i in range(10):
        vault.set_route_outcome(db, 100 + i, "down")
    for i in range(6):
        send(db, "google_ai_studio", 400, message_id=200 + i)
        vault.set_route_outcome(db, 200 + i, "up")
    learner.invalidate(db)
    pinkwave.activate(db, pinkwave.ChaosSettings(gain=0.0))
    decision = select_milp_endpoint("chat", 500, entropy_by_endpoint=ZERO)
    assert decision.endpoint.name == "google_ai_studio" and decision.learned["quality"] > 0.5 and "learner speed=" in decision.reason
    assert decision.explored is False and decision.runner_up == "groq"
    # Hard limits win over the best learned score: with gemini's window exhausted the request goes to groq.
    usage = {"google_ai_studio": {"rpm_used": 2, "tpm_used": 0}}
    forced = select_milp_endpoint("chat", 500, entropy_by_endpoint=ZERO, current_usage=usage)
    assert forced.endpoint.name == "groq"
    with pytest.raises(router.ProviderError):
        select_milp_endpoint("chat", 500, entropy_by_endpoint=ZERO, current_usage={"google_ai_studio": {"rpm_used": 2, "tpm_used": 0}, "groq": {"rpm_used": 30, "tpm_used": 0}})
    _ = gemini


# ----------------------------------------------------------------------------- step 2 · exploration engine

def test_exploration_frequency_matches_the_wave_parameters_over_one_walk(db):
    for rate in (0.10, 0.05, 0.25):
        chaos = pinkwave.Chaos(db, pinkwave.ChaosSettings(gain=1.0))
        hits = sum(1 for step in range(pinkwave.LENGTH) if chaos.explore(step, rate))
        assert hits == round(rate * pinkwave.LENGTH)
    half = pinkwave.Chaos(db, pinkwave.ChaosSettings(gain=0.5))
    assert sum(1 for step in range(pinkwave.LENGTH) if half.explore(step, 0.10)) == round(0.10 * pinkwave.LENGTH)  # the rate given is the rate walked
    assert not any(pinkwave.Chaos(db, pinkwave.ChaosSettings(gain=0.0)).explore(step, 0.5) for step in range(64))
    assert not any(pinkwave.Chaos(db, pinkwave.ChaosSettings(gain=1.0)).explore(step, 0.0) for step in range(64))
    # Pink correlation: explorations cluster, so the run of consecutive explored steps beats a memoryless coin.
    chaos = pinkwave.Chaos(db, pinkwave.ChaosSettings(gain=1.0))
    flags = [chaos.explore(step, 0.10) for step in range(pinkwave.LENGTH)]
    pairs = sum(1 for a, b in zip(flags, flags[1:]) if a and b)
    assert pairs > 0.10 * 0.10 * pinkwave.LENGTH * 2  # independent flips would give about 10 adjacent pairs


def test_the_selector_explores_the_runner_up_at_the_configured_share_and_logs_it(db):
    steps = iter(range(1, 20_000))
    chaos = pinkwave.activate(db, pinkwave.ChaosSettings(gain=1.0))
    chaos._step_source = lambda feature: next(steps)
    learner.save_settings(db, True, 0.10, ())
    explored = 0
    for _ in range(pinkwave.LENGTH):
        decision = select_milp_endpoint("chat", 500, entropy_by_endpoint=ZERO)
        assert decision.endpoint.name in ("groq", "google_ai_studio") and "huggingface" not in decision.reason.split("selected")[1][:12]
        if decision.explored:
            explored += 1
            assert decision.endpoint.name == "google_ai_studio" and decision.runner_up == "groq" and "explored runner-up" in decision.reason
        else:
            assert decision.endpoint.name == "groq"
        send(db, decision.endpoint.name, 300, explored=decision.explored, gain=1.0)
    assert explored == round(0.10 * pinkwave.LENGTH)
    stats = vault.exploration_stats(db)
    assert stats["explored"] == explored and abs(stats["observed_rate"] - 0.10) < 1e-3
    report = learner.report(db, ["chat"])
    assert report["exploration"]["configured_rate"] == pytest.approx(0.10 * pinkwave.settings_for(db).gain)
    assert any(r["endpoint"] == "groq" and r["latency obs"] > 0 for r in report["rows"])
    comparison = {r["setting"]: r for r in vault.chaos_comparison(db)}
    assert comparison["chaos on"]["explored"] == explored
    assert vault.routes_csv(db).splitlines()[0].endswith(",explored")


def test_exploration_respects_the_regret_bound_the_task_exclusion_and_feasibility(db):
    view = learner.Learner(db, learner.LearnerSettings(enabled=True, explore_max=0.5, laws=()))
    groq, gemini, hf = CORTEX_ENDPOINTS["groq"], CORTEX_ENDPOINTS["google_ai_studio"], CORTEX_ENDPOINTS["huggingface"]
    utilities = {"groq": 0.9, "google_ai_studio": 0.8, "huggingface": 0.4}
    assert view.choose_exploration([groq, gemini, hf], utilities, "groq", "chat") is gemini  # inside the bound, hf is not
    assert view.choose_exploration([groq, gemini, hf], utilities, "groq", "context_load") is None
    assert view.choose_exploration([groq], utilities, "groq", "chat") is None
    assert view.choose_exploration([groq, hf], utilities, "groq", "chat") is None  # only a runner-up outside the bound
    assert view.explore_rate(1.0) == 0.5 and view.explore_rate(0.2) == pytest.approx(0.1) and view.explore_rate(0.0) == 0.0
    chaos = pinkwave.activate(db, pinkwave.ChaosSettings(gain=1.0))
    chaos._step_source = lambda feature: 1
    learner.save_settings(db, True, 0.5, ())
    for _ in range(50):  # context_load never explores whatever the wave says
        assert select_milp_endpoint("context_load", 2_000, entropy_by_endpoint=ZERO).explored is False
    learner.save_settings(db, False, 0.5, ())
    assert learner.current() is None and select_milp_endpoint("chat", 500, entropy_by_endpoint=ZERO).learned == {}


# ----------------------------------------------------------------------------- step 3 · dynamics and laws

def test_constraint_laws_are_bounded_and_shape_the_right_endpoints():
    state = dynamics.SystemState(
        telemetry={"a": {"failure_rate": 1.0, "latency_ratio": 2.0, "observations": 10}, "b": {"failure_rate": 0.0, "latency_ratio": 0.0, "observations": 10}, "c": {"failure_rate": 0.0, "latency_ratio": 0.0, "observations": 0}},
        load={"a": 1.0, "b": 1.0, "c": 0.0}, last_used="b", coupling={("a", "c"): 0.9, ("b", "c"): 0.2},
    )
    assert dynamics.law_dissipation(state, "a") == -dynamics.PHYSICS_MAX and dynamics.law_dissipation(state, "b") == 0.0
    assert dynamics.law_friction(state, "a") == -dynamics.PHYSICS_MAX and dynamics.law_friction(state, "c") == 0.0
    assert dynamics.law_momentum(state, "b") == dynamics.MOMENTUM_BONUS and dynamics.law_momentum(state, "a") == 0.0
    assert dynamics.law_coupling(state, "c") < 0.0 and dynamics.law_coupling(state, "b") == 0.0  # c inherits from degraded a; b's tie is below the threshold
    total = dynamics.modulation(state, ["a", "b", "c"])
    assert total["a"] == -dynamics.PHYSICS_MAX and 0.0 < total["b"] <= dynamics.PHYSICS_MAX and all(-dynamics.PHYSICS_MAX <= v <= dynamics.PHYSICS_MAX for v in total.values())
    assert dynamics.modulation(state, ["a"], ()) == {"a": 0.0} and dynamics.modulation(state, ["b"], ("momentum",)) == {"b": dynamics.MOMENTUM_BONUS}
    groq = CORTEX_ENDPOINTS["groq"]
    assert dynamics.load_fraction(groq, EndpointUsage(rpm_used=15)) == 0.5 and dynamics.load_fraction(groq, EndpointUsage(rpm_used=3, tpm_used=8_000)) == 1.0
    assert dynamics.load_fraction(groq, None) == 0.0


def test_pairwise_statistics_find_correlation_lag_and_information_flow():
    import random

    rng = random.Random(3)
    a = [rng.gauss(0, 1) for _ in range(120)]
    lagged = [0.0] + a[:-1]                                        # b follows a one bin later
    noise = [rng.gauss(0, 1) for _ in range(120)]
    report = dynamics.pairwise_statistics({"a": a, "b": lagged, "n": noise}, prefer_pyspi=False)
    assert report.backend == "builtin" and report.bins == 120 and set(report.pairs) == {("a", "b"), ("a", "n"), ("b", "n")}
    ab = report.pairs[("a", "b")]
    assert ab["xcorr_max"] > 0.9 and ab["xcorr_lag"] == 1 and ab["te_ab"] > 0.5 and ab["te_ab"] > ab["te_ba"]  # a's past decides b: information flows a -> b
    assert abs(report.pairs[("a", "n")]["pearson"]) < 0.3
    rows = {r["pair"]: r for r in report.rows()}
    assert rows["a ~ b"]["at lag"] == 1 and rows["a ~ b"]["information flow"] == "a leads"
    assert dynamics.pairwise_statistics({"a": a[:4], "b": lagged[:4]}, prefer_pyspi=False).backend == "none"
    assert dynamics.transfer_entropy([1, 2, 3], [1, 2, 3]) == 0.0 and dynamics.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert dynamics.pyspi_available() in (True, False)


def test_endpoint_series_and_coupling_come_from_the_outcome_log(db, monkeypatch):
    now = time.time()
    with vault._open_database() as connection:
        for k in range(10):
            stamp = now - (10 - k) * 300 - 5
            for endpoint, ms in (("groq", 300 + 100 * (k % 3)), ("google_ai_studio", 600 + 100 * (k % 3))):
                connection.execute(
                    "INSERT INTO route_log (timestamp, project_scope, workspace, task_type, route, mode, ms, finish, reason) VALUES (?, ?, 'normal_chat', 'chat', ?, 'normal', ?, '', '')",
                    (stamp, db, f"{endpoint}/m", ms),
                )
        connection.commit()
    series = dynamics.endpoint_series(db, hours=1.0)
    assert set(series) == {"groq", "google_ai_studio"} and len(series["groq"]) == len(series["google_ai_studio"]) >= 10
    coupling = dynamics.cached_coupling(db)
    assert coupling[("google_ai_studio", "groq")] > 0.9  # the two move together bin for bin
    monkeypatch.setattr(dynamics, "endpoint_series", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no log")))
    dynamics.reset_for_tests()
    assert dynamics.cached_coupling(db) == {}


# ----------------------------------------------------------------------------- step 4 · settings and decisions carry the state

def test_learner_settings_persist_per_scope_and_decisions_carry_the_flags(db):
    saved = learner.save_settings(db, False, 0.9, ["friction", "bogus"])
    assert saved.enabled is False and saved.explore_max == 0.5 and saved.laws == ("friction",)
    assert learner.settings_for(db).laws == ("friction",) and learner.settings_for("other").enabled is True
    assert RouteDecision("p", "m", "chat", "r").explored is False
    row_id = vault.record_route(db, "normal_chat", "chat", "groq/m", "normal", 100, "", "r", explored=True)
    assert row_id and int(vault.recent_routes(db, limit=1)[0]["explored"]) == 1
