"""Smoke tests for the isolated Project Seth Phase 3 research engine.

These run a deliberately tiny configuration so CI stays fast. They check
reproducibility and numerical sanity only; they make no physical claims.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.router import advance_stochastic_project_seth_step
from research import project_seth_phase3 as p3

SMOKE = p3.ProjectSethConfig(
    dt=0.01,
    total_time=1.0,
    n_trajectories=6,
    master_seed_values=(20260910,),
    C_drive_values=(0.0, 0.18),
    save_csv=False,
    verbose=False,
)


def test_smoke_config_is_valid():
    p3.validate_config(SMOKE)


def test_powerlaw_noise_is_standardized_and_pink():
    rng = np.random.default_rng(7)
    samples, alpha_hat, fit_points = p3.generate_powerlaw_noise(
        n_steps=4096, dt=0.01, alpha=1.0, low_frequency_hz=0.2, high_frequency_hz=None, rng=rng
    )
    assert samples.shape == (4096,)
    assert abs(float(np.mean(samples))) < 1e-9
    assert abs(float(np.std(samples)) - 1.0) < 1e-9
    assert fit_points >= 4
    assert 0.6 < alpha_hat < 1.4


def test_run_condition_is_reproducible_and_finite():
    first = p3.run_condition(SMOKE, replication_seed=SMOKE.master_seed_values[0], C_drive=0.18)
    second = p3.run_condition(SMOKE, replication_seed=SMOKE.master_seed_values[0], C_drive=0.18)
    step_summary, smooth_summary, delta, results = first
    assert step_summary.track_name != smooth_summary.track_name
    assert len(results) == 2 * SMOKE.n_trajectories
    assert all(math.isfinite(result.final_x) for result in results)
    assert [r.final_x for r in results] == [r.final_x for r in second[3]]
    assert delta.n_trajectories == SMOKE.n_trajectories
    assert math.isfinite(delta.delta_mean_final_x_step_minus_quintic)


def test_null_control_runs():
    _, _, _, results = p3.run_condition(SMOKE, replication_seed=1, C_drive=0.0)
    assert len(results) == 2 * SMOKE.n_trajectories


def test_router_step_matches_research_stencil():
    x, eta, dt, sigma, a, c = -0.4, 0.7, 0.01, 0.2, 1.1, 0.03
    research = x + (p3.drift(x, a, c) * dt) + sigma * p3.noise_gate(x) * eta * math.sqrt(dt)
    router = advance_stochastic_project_seth_step(x, eta, dt, sigma, a, c)
    assert abs(float(research) - router) < 1e-12
