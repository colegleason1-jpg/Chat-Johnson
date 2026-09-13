#!/usr/bin/env python3
"""
PROJECT SETH — Phase 3 Distribution and Bias-Sweep Engine

Purpose
-------
Evaluate hard-step versus quintic transition topology in the scalar controlled
stochastic model:

    x[k+1] = x[k]
             + [A[k] * (x[k] - x[k]^3) + C[k]] * dt
             + sigma * (1 + |x[k]|) * eta[k] * sqrt(dt)

where A(t) = W(t) * T(t) / I(t).

This version adds:
    1. Final-state distribution metrics:
       mean, standard deviation, 95% CI, positive/negative/zero occupancy,
       sign imbalance, and hard-step-minus-quintic deltas.
    2. Independent master-seed replications.
    3. A predeclared C_drive sensitivity sweep, including the C_drive = 0
       directional null control.
    4. Paired forcing: for a given seed replication and trajectory index,
       Track A and Track B receive the identical colored-noise realization.
       The only intended difference is transition topology.
    5. Pre-exit and total protocol-work summaries.
    6. CSV output for run-level and sweep-level forensic analysis.

Scientific scope
----------------
This is a numerical experiment on a scalar stochastic model. The reported
observables are simulated state, first-exit, spectral, and protocol-work
statistics. They do not establish physical propulsion, force closure, metric
engineering, or any physical mechanism outside the stated model.

Requirements
------------
Python 3.10+
numpy

Colab quick start
-----------------
Paste this full script in one cell. Then run:

    results = main()

Default settings run 5 independent master seeds, 9 C_drive values, 2 topology
tracks, and 250 trajectories per condition. For a first smoke test, set:

    n_trajectories: int = 50
    master_seed_values: Tuple[int, ...] = (20260910,)
    C_drive_values: Tuple[float, ...] = (0.0, 0.18)

Then restore the default Phase 3 configuration for the full sensitivity pass.
"""

from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np


# =============================================================================
# Configuration and result records
# =============================================================================

@dataclass(frozen=True)
class ProjectSethConfig:
    # Integration.
    dt: float = 0.001
    total_time: float = 5.0
    n_trajectories: int = 250
    initial_x: float = -1.0
    sigma: float = 0.20

    # Finite FFT forcing: PSD S(f) proportional to 1 / f^alpha.
    noise_alpha: float = 1.0
    noise_low_frequency_hz: float = 0.20
    noise_high_frequency_hz: Optional[float] = None

    # First-exit boundaries.
    lower_exit_boundary: float = -1.50
    upper_exit_boundary: float = 0.75

    # Opening and capture protocol timing.
    t_a: float = 0.05
    t_b: float = 0.25
    t_capture_start: float = 0.60
    t_capture_end: float = 0.80

    # Raw channels used to construct A(t) = W(t) * T(t) / I(t).
    W_hold: float = 1.00
    W_open: float = 0.30
    W_capture: float = 1.15

    T_hold: float = 1.00
    T_open: float = 0.45
    T_capture: float = 1.00

    I_hold: float = 1.00
    I_open: float = 1.00
    I_capture: float = 1.00

    # C_hold and C_capture are fixed; C_drive is supplied by the sweep.
    C_hold: float = 0.00
    C_capture: float = 0.03

    # Independent replication seeds.
    master_seed_values: Tuple[int, ...] = (
        20260910,
        20260911,
        20260912,
        20260913,
        20260914,
    )

    # Predeclared directional-bias sweep. Zero is the directional null control.
    C_drive_values: Tuple[float, ...] = (
        -0.18,
        -0.12,
        -0.06,
        0.00,
        0.06,
        0.12,
        0.18,
        0.24,
        0.30,
    )

    output_directory: str = "project_seth_phase3_output"
    save_csv: bool = True
    save_example_trajectories: int = 0
    verbose: bool = True


@dataclass
class TrajectoryResult:
    replication_seed: int
    C_drive: float
    track_name: str
    use_idealized_step: bool
    trajectory_id: int
    trajectory_seed: int
    exited: bool
    exit_time: float
    exit_side: str
    exit_step: int
    final_x: float
    final_state_sign: int
    final_positive_indicator: int
    final_negative_indicator: int
    final_zero_indicator: int
    final_abs_x: float
    max_x: float
    min_x: float
    protocol_work_total: float
    protocol_work_abs_total: float
    protocol_work_pre_exit: float
    protocol_work_abs_pre_exit: float
    psd_alpha_estimate: float
    psd_fit_points: int


@dataclass
class TrackSummary:
    replication_seed: int
    C_drive: float
    track_name: str
    use_idealized_step: bool
    n_trajectories: int
    exit_fraction: float
    upper_exit_fraction: float
    lower_exit_fraction: float
    no_exit_fraction: float
    fraction_exiting_after_opening: float
    mean_first_exit_time: float
    median_first_exit_time: float
    first_exit_time_std: float
    first_exit_time_ci95_low: float
    first_exit_time_ci95_high: float
    mean_total_protocol_work: float
    median_total_protocol_work: float
    total_protocol_work_std: float
    total_protocol_work_ci95_low: float
    total_protocol_work_ci95_high: float
    mean_abs_total_protocol_work: float
    mean_pre_exit_protocol_work: float
    median_pre_exit_protocol_work: float
    pre_exit_protocol_work_std: float
    pre_exit_protocol_work_ci95_low: float
    pre_exit_protocol_work_ci95_high: float
    mean_abs_pre_exit_protocol_work: float
    mean_final_x: float
    final_x_std: float
    final_x_ci95_low: float
    final_x_ci95_high: float
    final_positive_fraction: float
    final_negative_fraction: float
    final_zero_fraction: float
    final_sign_imbalance: float
    mean_abs_final_x: float
    mean_max_x: float
    mean_min_x: float
    mean_noise_alpha_estimate: float
    mean_noise_alpha_abs_error: float
    mean_A: float
    min_A: float
    max_A: float


@dataclass
class PairedDelta:
    replication_seed: int
    C_drive: float
    n_trajectories: int
    delta_mean_final_x_step_minus_quintic: float
    delta_final_positive_fraction_step_minus_quintic: float
    delta_final_sign_imbalance_step_minus_quintic: float
    delta_upper_exit_fraction_step_minus_quintic: float
    delta_mean_first_exit_time_step_minus_quintic: float
    delta_mean_pre_exit_work_step_minus_quintic: float
    delta_mean_abs_pre_exit_work_step_minus_quintic: float
    delta_mean_total_work_step_minus_quintic: float


@dataclass
class SweepAggregate:
    C_drive: float
    n_replications: int
    n_trajectories_per_replication: int
    mean_delta_final_x: float
    delta_final_x_ci95_low: float
    delta_final_x_ci95_high: float
    positive_delta_final_x_fraction: float
    mean_delta_final_positive_fraction: float
    delta_positive_fraction_ci95_low: float
    delta_positive_fraction_ci95_high: float
    mean_delta_final_sign_imbalance: float
    delta_sign_imbalance_ci95_low: float
    delta_sign_imbalance_ci95_high: float
    mean_delta_upper_exit_fraction: float
    delta_upper_exit_fraction_ci95_low: float
    delta_upper_exit_fraction_ci95_high: float
    mean_delta_first_exit_time: float
    delta_first_exit_time_ci95_low: float
    delta_first_exit_time_ci95_high: float
    mean_delta_pre_exit_work: float
    delta_pre_exit_work_ci95_low: float
    delta_pre_exit_work_ci95_high: float
    mean_delta_abs_pre_exit_work: float
    delta_abs_pre_exit_work_ci95_low: float
    delta_abs_pre_exit_work_ci95_high: float
    mean_delta_total_work: float
    delta_total_work_ci95_low: float
    delta_total_work_ci95_high: float


# =============================================================================
# Validation and numerical utilities
# =============================================================================

def validate_config(config: ProjectSethConfig) -> None:
    if config.dt <= 0.0:
        raise ValueError("dt must be strictly positive.")
    if config.total_time <= 0.0:
        raise ValueError("total_time must be strictly positive.")
    if config.n_trajectories < 1:
        raise ValueError("n_trajectories must be at least 1.")
    if config.sigma < 0.0:
        raise ValueError("sigma must be non-negative.")
    if config.noise_alpha < 0.0:
        raise ValueError("noise_alpha must be non-negative.")
    if config.noise_low_frequency_hz <= 0.0:
        raise ValueError("noise_low_frequency_hz must be positive.")
    if not (
        0.0 <= config.t_a < config.t_b
        <= config.t_capture_start < config.t_capture_end
        <= config.total_time
    ):
        raise ValueError(
            "Timing must satisfy 0 <= t_a < t_b <= t_capture_start < "
            "t_capture_end <= total_time."
        )
    if config.lower_exit_boundary >= config.upper_exit_boundary:
        raise ValueError("lower_exit_boundary must be less than upper_exit_boundary.")
    if min(config.I_hold, config.I_open, config.I_capture) <= 0.0:
        raise ValueError("All inertia values must be strictly positive.")
    if not config.master_seed_values:
        raise ValueError("master_seed_values must contain at least one seed.")
    if not config.C_drive_values:
        raise ValueError("C_drive_values must contain at least one value.")

    n_steps = int(round(config.total_time / config.dt))
    if not np.isclose(n_steps * config.dt, config.total_time, rtol=0.0, atol=1e-12):
        raise ValueError("total_time must be an integer multiple of dt.")


def confidence_interval_95(values: np.ndarray) -> Tuple[float, float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float("nan"), float("nan")
    if values.size == 1:
        return float(values[0]), float(values[0])
    mean = float(np.mean(values))
    sem = float(np.std(values, ddof=1) / math.sqrt(values.size))
    margin = 1.96 * sem
    return mean - margin, mean + margin


def format_float(value: float, digits: int = 6) -> str:
    return "nan" if not np.isfinite(value) else f"{value:.{digits}f}"


# =============================================================================
# Core model and protocol
# =============================================================================

def control_coefficient(
    W_t: float | np.ndarray,
    T_t: float | np.ndarray,
    I_t: float | np.ndarray,
):
    """Exact control coefficient: A(t) = W(t) * T(t) / I(t)."""
    W = np.asarray(W_t, dtype=float)
    T = np.asarray(T_t, dtype=float)
    I = np.asarray(I_t, dtype=float)
    if np.any(I <= 0.0):
        raise ZeroDivisionError("I(t) must remain strictly positive.")
    A = (W * T) / I
    if not np.all(np.isfinite(A)):
        raise FloatingPointError("A(t) became non-finite.")
    return A


def drift(
    x: float | np.ndarray,
    A_t: float | np.ndarray,
    C_t: float | np.ndarray,
):
    """b(x,t) = A(t)*(x - x^3) + C(t)."""
    x_arr = np.asarray(x, dtype=float)
    return np.asarray(A_t, dtype=float) * (x_arr - x_arr**3) + np.asarray(
        C_t, dtype=float
    )


def noise_gate(x: float | np.ndarray):
    """State-dependent gate g(x) = 1 + |x|."""
    return 1.0 + np.abs(np.asarray(x, dtype=float))


def quintic_smootherstep(tau: float | np.ndarray):
    """s(tau)=6*tau^5-15*tau^4+10*tau^3, clamped to [0,1]."""
    z = np.clip(np.asarray(tau, dtype=float), 0.0, 1.0)
    return z**3 * (z * (z * 6.0 - 15.0) + 10.0)


def transition_fraction(
    time: np.ndarray,
    t_start: float,
    t_end: float,
    use_idealized_step: bool,
) -> np.ndarray:
    """Hard step for Track A; quintic transition over [t_start, t_end] for Track B."""
    t = np.asarray(time, dtype=float)
    if use_idealized_step:
        return (t >= t_start).astype(float)
    if t_end <= t_start:
        raise ValueError("Smooth transition requires t_end > t_start.")
    return quintic_smootherstep((t - t_start) / (t_end - t_start))


def transition_rate(
    time: np.ndarray,
    t_start: float,
    t_end: float,
    use_idealized_step: bool,
) -> np.ndarray:
    """Finite derivative diagnostic for the quintic track."""
    t = np.asarray(time, dtype=float)
    if use_idealized_step:
        return np.zeros_like(t, dtype=float)
    z = (t - t_start) / (t_end - t_start)
    mask = (t > t_start) & (t < t_end)
    rate = np.zeros_like(t, dtype=float)
    rate[mask] = 30.0 * z[mask] ** 2 * (1.0 - z[mask]) ** 2 / (t_end - t_start)
    return rate


def interpolate(q: np.ndarray, initial: float, final: float) -> np.ndarray:
    return initial + q * (final - initial)


def generate_protocol(
    time: np.ndarray,
    config: ProjectSethConfig,
    C_drive: float,
    use_idealized_step: bool,
) -> Dict[str, np.ndarray]:
    """Generate W, T, I, C, and derived A for one topology and bias setting."""
    q_open = transition_fraction(time, config.t_a, config.t_b, use_idealized_step)
    q_capture = transition_fraction(
        time,
        config.t_capture_start,
        config.t_capture_end,
        use_idealized_step,
    )

    W_open = interpolate(q_open, config.W_hold, config.W_open)
    T_open = interpolate(q_open, config.T_hold, config.T_open)
    I_open = interpolate(q_open, config.I_hold, config.I_open)
    C_open = interpolate(q_open, config.C_hold, C_drive)

    W = W_open + q_capture * (config.W_capture - W_open)
    T = T_open + q_capture * (config.T_capture - T_open)
    I = I_open + q_capture * (config.I_capture - I_open)
    C = C_open + q_capture * (config.C_capture - C_open)
    A = control_coefficient(W, T, I)

    if np.any(I <= 0.0) or not np.all(np.isfinite(A)):
        raise FloatingPointError("Invalid protocol state.")
    if not np.allclose(A, W * T / I, rtol=1e-12, atol=1e-14):
        raise AssertionError("A(t)=W(t)*T(t)/I(t) invariant failed.")

    return {
        "time": np.asarray(time, dtype=float),
        "W": W,
        "T": T,
        "I": I,
        "A": A,
        "C": C,
        "q_open": q_open,
        "q_capture": q_capture,
        "dq_open_dt": transition_rate(time, config.t_a, config.t_b, use_idealized_step),
        "dq_capture_dt": transition_rate(
            time,
            config.t_capture_start,
            config.t_capture_end,
            use_idealized_step,
        ),
    }


# =============================================================================
# Finite FFT colored-noise generation
# =============================================================================

def estimate_power_spectral_exponent(
    signal: np.ndarray,
    dt: float,
    low_frequency_hz: float,
    high_frequency_hz: float,
) -> Tuple[float, int]:
    values = np.asarray(signal, dtype=float)
    centered = values - np.mean(values)
    spectrum = np.fft.rfft(centered)
    frequencies = np.fft.rfftfreq(values.size, d=dt)
    psd = (np.abs(spectrum) ** 2) / values.size
    mask = (
        (frequencies >= low_frequency_hz)
        & (frequencies <= high_frequency_hz)
        & (frequencies > 0.0)
        & (psd > 0.0)
        & np.isfinite(psd)
    )
    if int(np.sum(mask)) < 4:
        return float("nan"), int(np.sum(mask))
    slope, _ = np.polyfit(np.log(frequencies[mask]), np.log(psd[mask]), deg=1)
    return -float(slope), int(np.sum(mask))


def generate_powerlaw_noise(
    n_steps: int,
    dt: float,
    alpha: float,
    low_frequency_hz: float,
    high_frequency_hz: Optional[float],
    rng: np.random.Generator,
) -> Tuple[np.ndarray, float, int]:
    """Generate finite zero-mean unit-variance forcing with PSD proportional to 1/f^alpha."""
    nyquist_hz = 0.5 / dt
    f_high = nyquist_hz if high_frequency_hz is None else min(high_frequency_hz, nyquist_hz)
    if not (0.0 < low_frequency_hz < f_high):
        raise ValueError("Require 0 < low_frequency_hz < high_frequency_hz <= Nyquist.")

    white = rng.normal(0.0, 1.0, size=n_steps)
    spectrum = np.fft.rfft(white)
    frequencies = np.fft.rfftfreq(n_steps, d=dt)
    amplitude = np.zeros_like(frequencies, dtype=float)
    band = (
        (frequencies >= low_frequency_hz)
        & (frequencies <= f_high)
        & (frequencies > 0.0)
    )
    amplitude[band] = frequencies[band] ** (-0.5 * alpha)
    amplitude[0] = 0.0

    colored = np.fft.irfft(spectrum * amplitude, n=n_steps)
    std = float(np.std(colored))
    if not np.isfinite(std) or std <= np.finfo(float).eps:
        raise FloatingPointError("Invalid colored-noise variance.")
    colored = (colored - float(np.mean(colored))) / std

    alpha_hat, fit_points = estimate_power_spectral_exponent(
        colored,
        dt,
        low_frequency_hz,
        f_high,
    )
    return colored, alpha_hat, fit_points


# =============================================================================
# Trajectory dynamics and diagnostics
# =============================================================================

def protocol_work_increment(
    x_k: float,
    A_before: float,
    C_before: float,
    A_after: float,
    C_after: float,
) -> float:
    """Finite parameter-change work at fixed state x_k."""
    shape_term = 0.5 * x_k**2 - 0.25 * x_k**4
    return float(
        -(A_after - A_before) * shape_term - (C_after - C_before) * x_k
    )


def exit_side(x: float, lower_boundary: float, upper_boundary: float) -> Optional[str]:
    if x >= upper_boundary:
        return "upper"
    if x <= lower_boundary:
        return "lower"
    return None


def simulate_trajectory(
    config: ProjectSethConfig,
    protocol: Dict[str, np.ndarray],
    replication_seed: int,
    C_drive: float,
    track_name: str,
    use_idealized_step: bool,
    trajectory_id: int,
    trajectory_seed: int,
) -> TrajectoryResult:
    """Advance one full trajectory using a paired seed shared across the two tracks."""
    time = protocol["time"]
    A = protocol["A"]
    C = protocol["C"]
    n_steps = time.size - 1

    rng = np.random.default_rng(trajectory_seed)
    eta, alpha_hat, fit_points = generate_powerlaw_noise(
        n_steps=n_steps,
        dt=config.dt,
        alpha=config.noise_alpha,
        low_frequency_hz=config.noise_low_frequency_hz,
        high_frequency_hz=config.noise_high_frequency_hz,
        rng=rng,
    )

    x = float(config.initial_x)
    max_x = x
    min_x = x
    exited = False
    exit_time = float("nan")
    exit_side_value = "none"
    exit_step = -1
    work_total = 0.0
    work_abs_total = 0.0
    work_pre_exit = 0.0
    work_abs_pre_exit = 0.0
    sqrt_dt = math.sqrt(config.dt)

    for k in range(n_steps):
        A_k = float(A[k])
        C_k = float(C[k])
        deterministic = float(drift(x, A_k, C_k))
        diffusion = config.sigma * float(noise_gate(x)) * eta[k] * sqrt_dt
        x_next = x + deterministic * config.dt + diffusion

        if not np.isfinite(x_next):
            raise FloatingPointError(
                f"Non-finite state: seed={replication_seed}, C={C_drive}, "
                f"track={track_name}, trajectory={trajectory_id}, step={k}."
            )

        d_work = protocol_work_increment(
            x_k=x,
            A_before=A_k,
            C_before=C_k,
            A_after=float(A[k + 1]),
            C_after=float(C[k + 1]),
        )
        work_total += d_work
        work_abs_total += abs(d_work)
        if not exited:
            work_pre_exit += d_work
            work_abs_pre_exit += abs(d_work)

        x = float(x_next)
        max_x = max(max_x, x)
        min_x = min(min_x, x)

        if not exited:
            side = exit_side(x, config.lower_exit_boundary, config.upper_exit_boundary)
            if side is not None:
                exited = True
                exit_time = float(time[k + 1])
                exit_side_value = side
                exit_step = k + 1

    return TrajectoryResult(
        replication_seed=replication_seed,
        C_drive=C_drive,
        track_name=track_name,
        use_idealized_step=use_idealized_step,
        trajectory_id=trajectory_id,
        trajectory_seed=trajectory_seed,
        exited=exited,
        exit_time=exit_time,
        exit_side=exit_side_value,
        exit_step=exit_step,
        final_x=x,
        final_state_sign=int(np.sign(x)),
        final_positive_indicator=int(x > 0.0),
        final_negative_indicator=int(x < 0.0),
        final_zero_indicator=int(x == 0.0),
        final_abs_x=abs(x),
        max_x=max_x,
        min_x=min_x,
        protocol_work_total=work_total,
        protocol_work_abs_total=work_abs_total,
        protocol_work_pre_exit=work_pre_exit,
        protocol_work_abs_pre_exit=work_abs_pre_exit,
        psd_alpha_estimate=alpha_hat,
        psd_fit_points=fit_points,
    )


def summarize_track(
    replication_seed: int,
    C_drive: float,
    track_name: str,
    use_idealized_step: bool,
    config: ProjectSethConfig,
    protocol: Dict[str, np.ndarray],
    results: List[TrajectoryResult],
) -> TrackSummary:
    n = len(results)
    exit_times = np.asarray([r.exit_time for r in results if r.exited], dtype=float)
    total_work = np.asarray([r.protocol_work_total for r in results], dtype=float)
    abs_total_work = np.asarray([r.protocol_work_abs_total for r in results], dtype=float)
    pre_exit_work = np.asarray([r.protocol_work_pre_exit for r in results], dtype=float)
    abs_pre_exit_work = np.asarray(
        [r.protocol_work_abs_pre_exit for r in results], dtype=float
    )
    final_x = np.asarray([r.final_x for r in results], dtype=float)
    alpha_hats = np.asarray([r.psd_alpha_estimate for r in results], dtype=float)

    tau_low, tau_high = confidence_interval_95(exit_times)
    total_work_low, total_work_high = confidence_interval_95(total_work)
    pre_exit_low, pre_exit_high = confidence_interval_95(pre_exit_work)
    final_x_low, final_x_high = confidence_interval_95(final_x)

    upper = sum(r.exit_side == "upper" for r in results)
    lower = sum(r.exit_side == "lower" for r in results)
    exits = upper + lower
    final_positive = sum(r.final_positive_indicator for r in results)
    final_negative = sum(r.final_negative_indicator for r in results)
    final_zero = sum(r.final_zero_indicator for r in results)
    exits_after_opening = sum(r.exited and r.exit_time >= config.t_a for r in results)

    return TrackSummary(
        replication_seed=replication_seed,
        C_drive=C_drive,
        track_name=track_name,
        use_idealized_step=use_idealized_step,
        n_trajectories=n,
        exit_fraction=exits / n,
        upper_exit_fraction=upper / n,
        lower_exit_fraction=lower / n,
        no_exit_fraction=(n - exits) / n,
        fraction_exiting_after_opening=exits_after_opening / n,
        mean_first_exit_time=float(np.mean(exit_times)) if exit_times.size else float("nan"),
        median_first_exit_time=float(np.median(exit_times)) if exit_times.size else float("nan"),
        first_exit_time_std=float(np.std(exit_times, ddof=1)) if exit_times.size > 1 else 0.0,
        first_exit_time_ci95_low=tau_low,
        first_exit_time_ci95_high=tau_high,
        mean_total_protocol_work=float(np.mean(total_work)),
        median_total_protocol_work=float(np.median(total_work)),
        total_protocol_work_std=float(np.std(total_work, ddof=1)) if n > 1 else 0.0,
        total_protocol_work_ci95_low=total_work_low,
        total_protocol_work_ci95_high=total_work_high,
        mean_abs_total_protocol_work=float(np.mean(abs_total_work)),
        mean_pre_exit_protocol_work=float(np.mean(pre_exit_work)),
        median_pre_exit_protocol_work=float(np.median(pre_exit_work)),
        pre_exit_protocol_work_std=float(np.std(pre_exit_work, ddof=1)) if n > 1 else 0.0,
        pre_exit_protocol_work_ci95_low=pre_exit_low,
        pre_exit_protocol_work_ci95_high=pre_exit_high,
        mean_abs_pre_exit_protocol_work=float(np.mean(abs_pre_exit_work)),
        mean_final_x=float(np.mean(final_x)),
        final_x_std=float(np.std(final_x, ddof=1)) if n > 1 else 0.0,
        final_x_ci95_low=final_x_low,
        final_x_ci95_high=final_x_high,
        final_positive_fraction=final_positive / n,
        final_negative_fraction=final_negative / n,
        final_zero_fraction=final_zero / n,
        final_sign_imbalance=(final_positive - final_negative) / n,
        mean_abs_final_x=float(np.mean(np.abs(final_x))),
        mean_max_x=float(np.mean([r.max_x for r in results])),
        mean_min_x=float(np.mean([r.min_x for r in results])),
        mean_noise_alpha_estimate=float(np.nanmean(alpha_hats)),
        mean_noise_alpha_abs_error=float(
            np.nanmean(np.abs(alpha_hats - config.noise_alpha))
        ),
        mean_A=float(np.mean(protocol["A"])),
        min_A=float(np.min(protocol["A"])),
        max_A=float(np.max(protocol["A"])),
    )


def paired_delta(
    step_summary: TrackSummary,
    smooth_summary: TrackSummary,
) -> PairedDelta:
    if step_summary.replication_seed != smooth_summary.replication_seed:
        raise ValueError("Cannot compare summaries from different replication seeds.")
    if not np.isclose(step_summary.C_drive, smooth_summary.C_drive):
        raise ValueError("Cannot compare summaries from different C_drive values.")

    return PairedDelta(
        replication_seed=step_summary.replication_seed,
        C_drive=step_summary.C_drive,
        n_trajectories=step_summary.n_trajectories,
        delta_mean_final_x_step_minus_quintic=(
            step_summary.mean_final_x - smooth_summary.mean_final_x
        ),
        delta_final_positive_fraction_step_minus_quintic=(
            step_summary.final_positive_fraction - smooth_summary.final_positive_fraction
        ),
        delta_final_sign_imbalance_step_minus_quintic=(
            step_summary.final_sign_imbalance - smooth_summary.final_sign_imbalance
        ),
        delta_upper_exit_fraction_step_minus_quintic=(
            step_summary.upper_exit_fraction - smooth_summary.upper_exit_fraction
        ),
        delta_mean_first_exit_time_step_minus_quintic=(
            step_summary.mean_first_exit_time - smooth_summary.mean_first_exit_time
        ),
        delta_mean_pre_exit_work_step_minus_quintic=(
            step_summary.mean_pre_exit_protocol_work
            - smooth_summary.mean_pre_exit_protocol_work
        ),
        delta_mean_abs_pre_exit_work_step_minus_quintic=(
            step_summary.mean_abs_pre_exit_protocol_work
            - smooth_summary.mean_abs_pre_exit_protocol_work
        ),
        delta_mean_total_work_step_minus_quintic=(
            step_summary.mean_total_protocol_work
            - smooth_summary.mean_total_protocol_work
        ),
    )


# =============================================================================
# Sweep execution
# =============================================================================

def build_trajectory_seeds(
    replication_seed: int,
    C_drive: float,
    n_trajectories: int,
) -> List[int]:
    """
    Build deterministic trajectory seeds for a replication and bias level.

    These seeds are intentionally shared by Track A and Track B, ensuring a
    paired-noise topology comparison for every trajectory index.
    """
    c_token = int(round((C_drive + 1.0) * 1_000_000))
    sequence = np.random.SeedSequence([replication_seed, c_token])
    children = sequence.spawn(n_trajectories)
    return [int(child.generate_state(1, dtype=np.uint64)[0]) for child in children]


def run_condition(
    config: ProjectSethConfig,
    replication_seed: int,
    C_drive: float,
) -> Tuple[TrackSummary, TrackSummary, PairedDelta, List[TrajectoryResult]]:
    n_steps = int(round(config.total_time / config.dt))
    time = np.linspace(0.0, config.total_time, n_steps + 1, dtype=float)

    step_protocol = generate_protocol(
        time=time,
        config=config,
        C_drive=C_drive,
        use_idealized_step=True,
    )
    smooth_protocol = generate_protocol(
        time=time,
        config=config,
        C_drive=C_drive,
        use_idealized_step=False,
    )

    trajectory_seeds = build_trajectory_seeds(
        replication_seed,
        C_drive,
        config.n_trajectories,
    )

    step_results: List[TrajectoryResult] = []
    smooth_results: List[TrajectoryResult] = []

    for trajectory_id, trajectory_seed in enumerate(trajectory_seeds):
        step_results.append(
            simulate_trajectory(
                config=config,
                protocol=step_protocol,
                replication_seed=replication_seed,
                C_drive=C_drive,
                track_name="track_a_idealized_step",
                use_idealized_step=True,
                trajectory_id=trajectory_id,
                trajectory_seed=trajectory_seed,
            )
        )
        smooth_results.append(
            simulate_trajectory(
                config=config,
                protocol=smooth_protocol,
                replication_seed=replication_seed,
                C_drive=C_drive,
                track_name="track_b_quintic_smooth",
                use_idealized_step=False,
                trajectory_id=trajectory_id,
                trajectory_seed=trajectory_seed,
            )
        )

    step_summary = summarize_track(
        replication_seed,
        C_drive,
        "track_a_idealized_step",
        True,
        config,
        step_protocol,
        step_results,
    )
    smooth_summary = summarize_track(
        replication_seed,
        C_drive,
        "track_b_quintic_smooth",
        False,
        config,
        smooth_protocol,
        smooth_results,
    )
    delta = paired_delta(step_summary, smooth_summary)

    return step_summary, smooth_summary, delta, step_results + smooth_results


def aggregate_deltas(deltas: List[PairedDelta]) -> SweepAggregate:
    if not deltas:
        raise ValueError("Cannot aggregate an empty delta set.")

    def aggregate(field: str) -> Tuple[float, float, float]:
        values = np.asarray([getattr(delta, field) for delta in deltas], dtype=float)
        low, high = confidence_interval_95(values)
        return float(np.mean(values)), low, high

    C_drive = deltas[0].C_drive
    if not all(np.isclose(delta.C_drive, C_drive) for delta in deltas):
        raise ValueError("All deltas in an aggregate must share C_drive.")

    mean_final_x, final_low, final_high = aggregate(
        "delta_mean_final_x_step_minus_quintic"
    )
    positive_final_fraction = float(
        np.mean(
            [
                delta.delta_mean_final_x_step_minus_quintic > 0.0
                for delta in deltas
            ]
        )
    )
    mean_positive_fraction, positive_low, positive_high = aggregate(
        "delta_final_positive_fraction_step_minus_quintic"
    )
    mean_imbalance, imbalance_low, imbalance_high = aggregate(
        "delta_final_sign_imbalance_step_minus_quintic"
    )
    mean_upper, upper_low, upper_high = aggregate(
        "delta_upper_exit_fraction_step_minus_quintic"
    )
    mean_tau, tau_low, tau_high = aggregate(
        "delta_mean_first_exit_time_step_minus_quintic"
    )
    mean_pre_exit_work, pre_low, pre_high = aggregate(
        "delta_mean_pre_exit_work_step_minus_quintic"
    )
    mean_abs_pre_exit_work, abs_pre_low, abs_pre_high = aggregate(
        "delta_mean_abs_pre_exit_work_step_minus_quintic"
    )
    mean_total_work, total_low, total_high = aggregate(
        "delta_mean_total_work_step_minus_quintic"
    )

    return SweepAggregate(
        C_drive=C_drive,
        n_replications=len(deltas),
        n_trajectories_per_replication=deltas[0].n_trajectories,
        mean_delta_final_x=mean_final_x,
        delta_final_x_ci95_low=final_low,
        delta_final_x_ci95_high=final_high,
        positive_delta_final_x_fraction=positive_final_fraction,
        mean_delta_final_positive_fraction=mean_positive_fraction,
        delta_positive_fraction_ci95_low=positive_low,
        delta_positive_fraction_ci95_high=positive_high,
        mean_delta_final_sign_imbalance=mean_imbalance,
        delta_sign_imbalance_ci95_low=imbalance_low,
        delta_sign_imbalance_ci95_high=imbalance_high,
        mean_delta_upper_exit_fraction=mean_upper,
        delta_upper_exit_fraction_ci95_low=upper_low,
        delta_upper_exit_fraction_ci95_high=upper_high,
        mean_delta_first_exit_time=mean_tau,
        delta_first_exit_time_ci95_low=tau_low,
        delta_first_exit_time_ci95_high=tau_high,
        mean_delta_pre_exit_work=mean_pre_exit_work,
        delta_pre_exit_work_ci95_low=pre_low,
        delta_pre_exit_work_ci95_high=pre_high,
        mean_delta_abs_pre_exit_work=mean_abs_pre_exit_work,
        delta_abs_pre_exit_work_ci95_low=abs_pre_low,
        delta_abs_pre_exit_work_ci95_high=abs_pre_high,
        mean_delta_total_work=mean_total_work,
        delta_total_work_ci95_low=total_low,
        delta_total_work_ci95_high=total_high,
    )


# =============================================================================
# CSV and console reporting
# =============================================================================

def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def print_aggregate_table(aggregates: List[SweepAggregate]) -> None:
    print("\nPHASE 3 PAIRED TOPOLOGY SWEEP")
    print("Hard step minus quintic; intervals are across independent master-seed replications.")
    print("=" * 154)
    print(
        f"{'C_drive':>9}"
        f"{'Δ mean final x':>18}"
        f"{'95% CI':>27}"
        f"{'P(Δx>0)':>12}"
        f"{'Δ pos frac':>15}"
        f"{'Δ upper exit':>16}"
        f"{'Δ mean tau':>15}"
        f"{'Δ pre-exit W':>18}"
        f"{'Δ |pre W|':>15}"
    )
    print("-" * 154)
    for row in aggregates:
        ci = f"[{format_float(row.delta_final_x_ci95_low)}, {format_float(row.delta_final_x_ci95_high)}]"
        print(
            f"{row.C_drive:>9.3f}"
            f"{row.mean_delta_final_x:>18.6f}"
            f"{ci:>27}"
            f"{row.positive_delta_final_x_fraction:>12.3f}"
            f"{row.mean_delta_final_positive_fraction:>15.6f}"
            f"{row.mean_delta_upper_exit_fraction:>16.6f}"
            f"{row.mean_delta_first_exit_time:>15.6f}"
            f"{row.mean_delta_pre_exit_work:>18.6f}"
            f"{row.mean_delta_abs_pre_exit_work:>15.6f}"
        )
    print("=" * 154)
    print(
        "Interpretation: a positive Δ mean final x means the hard-step ensemble ended "
        "more positive than its paired quintic ensemble under the specified numerical model."
    )


def main() -> Dict[str, object]:
    config = ProjectSethConfig()
    validate_config(config)

    output_dir = Path(config.output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)

    if config.verbose:
        conditions = (
            len(config.master_seed_values)
            * len(config.C_drive_values)
            * config.n_trajectories
            * 2
        )
        print("=" * 92)
        print("PROJECT SETH — PHASE 3 DISTRIBUTION AND BIAS-SWEEP ENGINE")
        print("=" * 92)
        print(
            f"dt={config.dt}, total_time={config.total_time}, "
            f"trajectories/condition={config.n_trajectories}"
        )
        print(
            f"replications={len(config.master_seed_values)}, "
            f"C_drive values={len(config.C_drive_values)}, "
            f"paired trajectory runs={conditions}"
        )
        print(
            f"opening=[{config.t_a}, {config.t_b}], "
            f"capture=[{config.t_capture_start}, {config.t_capture_end}], "
            f"alpha={config.noise_alpha}, sigma={config.sigma}"
        )

    track_summaries: List[TrackSummary] = []
    deltas: List[PairedDelta] = []
    trajectory_results: List[TrajectoryResult] = []

    for C_drive in config.C_drive_values:
        for replication_seed in config.master_seed_values:
            step_summary, smooth_summary, delta, condition_results = run_condition(
                config=config,
                replication_seed=replication_seed,
                C_drive=C_drive,
            )
            track_summaries.extend([step_summary, smooth_summary])
            deltas.append(delta)
            trajectory_results.extend(condition_results)

            if config.verbose:
                print(
                    f"completed C_drive={C_drive:+.3f}, seed={replication_seed}, "
                    f"Δ final mean x={delta.delta_mean_final_x_step_minus_quintic:+.6f}, "
                    f"Δ pre-exit work={delta.delta_mean_pre_exit_work_step_minus_quintic:+.6f}"
                )

    aggregates = [
        aggregate_deltas(
            [delta for delta in deltas if np.isclose(delta.C_drive, C_drive)]
        )
        for C_drive in config.C_drive_values
    ]

    print_aggregate_table(aggregates)

    if config.save_csv:
        write_csv(
            output_dir / "project_seth_phase3_configuration.csv",
            [asdict(config)],
        )
        write_csv(
            output_dir / "phase3_track_summaries.csv",
            [asdict(summary) for summary in track_summaries],
        )
        write_csv(
            output_dir / "phase3_paired_replication_deltas.csv",
            [asdict(delta) for delta in deltas],
        )
        write_csv(
            output_dir / "phase3_sweep_aggregates.csv",
            [asdict(aggregate) for aggregate in aggregates],
        )
        write_csv(
            output_dir / "phase3_trajectory_results.csv",
            [asdict(result) for result in trajectory_results],
        )
        print(f"\nCSV output written to: {output_dir.resolve()}")

    return {
        "config": config,
        "track_summaries": track_summaries,
        "paired_deltas": deltas,
        "sweep_aggregates": aggregates,
        "trajectory_results": trajectory_results,
    }


if __name__ == "__main__":
    main()
