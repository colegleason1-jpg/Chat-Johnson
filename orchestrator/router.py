"""Chat Johnson routing core.

This module keeps the original provider-compatible router and adds an explicit,
experimentally isolated Tri-Processor Cortex:

* Cortex 3 generates reproducible 1/f noise and integrates the Project Seth
  scalar SDE.  It is a routing signal, not a claim about physical propulsion.
* Cortex 2 uses a binary MILP when SciPy is installed.  Every selected request
  is constrained by the configured RPM/TPM ceilings before a provider call.
* Cortex 1 formats provider-specific HTTP payloads and exposes an SSE/text
  stream for Gemini, Groq, and Hugging Face.

API keys are BYOK: values are read from the process environment at call time,
never written to memory files, logs, prompts, or the local SQLite vault.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

try:
    import requests
except ImportError:  # pragma: no cover - the declared requirements include requests.
    requests = None  # type: ignore[assignment]

try:
    from .providers import ProviderError, chat as legacy_chat
except ModuleNotFoundError as exc:  # Keep pure routing/math helpers importable in a lean test env.
    if exc.name != "requests":
        raise

    class ProviderError(RuntimeError):
        """Raised when an endpoint cannot be called in the current environment."""

    def legacy_chat(*args: Any, **kwargs: Any) -> Tuple[str, int]:
        raise ProviderError("requests is required for provider HTTP execution")

from . import discovery
from . import providers as _providers
from .config import PROVIDERS, Settings, get_settings, provider_model, resolve_secret
from .quota import QuotaLedger

# Preserve the original module-level import surface for callers/tests that
# monkeypatch ``orchestrator.router.chat``.
chat = legacy_chat

try:  # Optional at import time so the existing router still works in lean installs.
    import numpy as np
except ImportError:  # pragma: no cover - exercised only by minimal installations.
    np = None  # type: ignore[assignment]

try:  # SciPy is required only for the exact MILP path.
    from scipy.optimize import Bounds, LinearConstraint, milp
except ImportError:  # pragma: no cover - deterministic fallback is tested instead.
    Bounds = LinearConstraint = milp = None  # type: ignore[assignment]


# =============================================================================
# BYOK vault and provider contracts
# =============================================================================

BYOK_ENV_KEYS: Dict[str, Tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "huggingface": ("HUGGINGFACE_API_KEY", "HF_TOKEN"),
}


def refresh_byok_vault() -> Dict[str, Dict[str, Any]]:
    """Read the current process environment into an in-memory BYOK schema.

    The returned dictionary intentionally contains the key only in process
    memory so the HTTP adapter can use it.  Callers should use
    :func:`byok_status` when displaying state; it never returns key values.
    """
    vault: Dict[str, Dict[str, Any]] = {}
    for provider, env_names in BYOK_ENV_KEYS.items():
        selected_name = ""
        selected_value = ""
        for env_name in env_names:
            value = resolve_secret(env_name)
            if value:
                selected_name = env_name
                selected_value = value
                break
        vault[provider] = {
            "provider": provider,
            "env_key": selected_name or env_names[0],
            "api_key": selected_value,
            "configured": bool(selected_value),
        }
    return vault


BYOK_VAULT: Dict[str, Dict[str, Any]] = refresh_byok_vault()


def byok_status() -> Dict[str, Dict[str, Any]]:
    """Return redacted BYOK availability information for a UI or CLI."""
    vault = refresh_byok_vault()
    return {
        name: {"env_key": row["env_key"], "configured": bool(row["configured"])}
        for name, row in vault.items()
    }


@dataclass(frozen=True)
class CortexEndpoint:
    """A provider contract used by Cortex 2 and Cortex 1."""

    name: str
    label: str
    env_keys: Tuple[str, ...]
    base_url: str
    kind: str
    model: str
    rpm_limit: int
    tpm_limit: Optional[int]
    speed_score: float
    context_score: float
    strengths: Tuple[str, ...]
    model_env: str = ""


# Shared with the legacy provider client: one live-id cache per vendor.
_DISCOVERED_MODELS = discovery.DISCOVERED
MODEL_PREFERENCES = {
    "google_ai_studio": discovery.VENDOR_PREFERENCES["gemini"],
    "groq": discovery.VENDOR_PREFERENCES["groq"],
    "huggingface": discovery.VENDOR_PREFERENCES["huggingface"],
}


def endpoint_model(endpoint: "CortexEndpoint") -> str:
    """Resolve the model id at call time: env override > discovered live id > default."""
    if endpoint.model_env:
        override = resolve_secret(endpoint.model_env)  # session overlay first, then the environment
        if override:
            return override
    return discovery.discovered(endpoint.name) or endpoint.model


looks_like_retired_model = discovery.looks_like_retired_model


# These are deliberate routing ceilings from the Project Seth / Chat Johnson
# design. They are conservative policy limits, not guarantees from vendors.
# Model ids age out (Groq retired llama-3.3-70b-versatile on 2026-08-16 and
# Google retired gemini-1.5-pro / gemini-2.0-flash), so ``endpoint_model``
# honours an env override first, then a live id discovered from the vendor's
# model list after a retirement error, then this default. Groq's free plan
# for gpt-oss-120b is 30 RPM / 8K TPM, hence the tighter TPM ceiling.
CORTEX_ENDPOINTS: Dict[str, CortexEndpoint] = {
    "google_ai_studio": CortexEndpoint(
        name="google_ai_studio",
        label="Google AI Studio / Gemini Flash",
        env_keys=("GEMINI_API_KEY",),
        base_url="https://generativelanguage.googleapis.com/v1beta",
        kind="gemini",
        model="gemini-3.6-flash",
        rpm_limit=2,
        tpm_limit=32_000,
        speed_score=0.48,
        context_score=1.00,
        strengths=("context_load", "reasoning", "chat"),
        model_env="CORTEX_GEMINI_MODEL",
    ),
    "groq": CortexEndpoint(
        name="groq",
        label="Groq / gpt-oss-120b",
        env_keys=("GROQ_API_KEY",),
        base_url="https://api.groq.com/openai/v1",
        kind="openai",
        model="openai/gpt-oss-120b",
        rpm_limit=30,
        tpm_limit=8_000,
        speed_score=1.00,
        context_score=0.58,
        strengths=("code_patch", "quick_text", "chat"),
        model_env="CORTEX_GROQ_MODEL",
    ),
    "huggingface": CortexEndpoint(
        name="huggingface",
        label="Hugging Face Serverless",
        env_keys=("HUGGINGFACE_API_KEY", "HF_TOKEN"),
        base_url="https://router.huggingface.co/v1",
        kind="openai",
        model="Qwen/Qwen2.5-Coder-32B-Instruct",
        rpm_limit=60,
        tpm_limit=None,
        speed_score=0.72,
        context_score=0.70,
        strengths=("code_patch", "test_fix", "reasoning"),
        model_env="CORTEX_HF_MODEL",
    ),
}


# =============================================================================
# Cortex 3 — Project Seth numerical routing signal
# =============================================================================

@dataclass
class PinkNoiseResult:
    samples: Any
    frequencies: Any
    power_spectrum: Any
    alpha_target: float
    alpha_estimate: float
    entropy: float


def _require_numpy() -> Any:
    if np is None:
        raise RuntimeError(
            "Cortex 3 requires numpy. Install the project requirements before "
            "running the Project Seth numerical probe."
        )
    return np


def shannon_entropy(values: Sequence[float], bins: Any = None) -> float:
    """Return histogram Shannon entropy H(x) in bits for a trajectory array.

    ``bins`` may be a count (histogram over the data's own range) or explicit bin
    edges (fixed range, so a wider distribution scores higher).
    """
    if np is None:
        finite = [float(value) for value in values if math.isfinite(float(value))]
        if not finite or min(finite) == max(finite):
            return 0.0
        count = max(8, min(128, int(math.sqrt(len(finite))) * 2)) if bins is None else bins
        low, high = min(finite), max(finite)
        counts = [0] * count
        for value in finite:
            index = min(count - 1, int((value - low) / (high - low) * count))
            counts[index] += 1
        total = float(len(finite))
        return float(-sum((n / total) * math.log2(n / total) for n in counts if n))

    array = np.asarray(values, dtype=float).reshape(-1)
    array = array[np.isfinite(array)]
    if array.size == 0 or float(array.max() - array.min()) == 0.0:
        return 0.0
    bin_spec: Any = bins if bins is not None else max(8, min(128, int(math.sqrt(array.size)) * 2))
    histogram, _ = np.histogram(array, bins=bin_spec)
    probabilities = histogram.astype(float) / float(array.size)
    probabilities = probabilities[probabilities > 0.0]
    return float(-np.sum(probabilities * np.log2(probabilities)))


def fit_one_over_f_alpha(
    frequencies: Sequence[float],
    power_spectrum: Sequence[float],
    low_frequency_hz: float,
    high_frequency_hz: Optional[float] = None,
) -> float:
    """Fit PSD ~ 1/f^alpha using a finite, log-log positive-frequency band."""
    if np is None:
        return float("nan")
    frequency_array = np.asarray(frequencies, dtype=float)
    power_array = np.asarray(power_spectrum, dtype=float)
    cutoff = max(float(low_frequency_hz), float(frequency_array[1]) if frequency_array.size > 1 else float(low_frequency_hz))
    mask = (frequency_array >= cutoff) & (power_array > 0.0) & np.isfinite(power_array)
    if high_frequency_hz is not None:
        mask &= frequency_array <= float(high_frequency_hz)
    if int(np.count_nonzero(mask)) < 2:
        return float("nan")
    slope, _ = np.polyfit(np.log(frequency_array[mask]), np.log(power_array[mask]), 1)
    return float(-slope)


ALPHA_FIT_REALIZATIONS = 8


def generate_one_over_f_noise(
    length: int,
    alpha: float = 1.0,
    seed: Optional[int] = None,
    sample_rate: float = 1.0,
    low_frequency_hz: float = 0.01,
    high_frequency_hz: Optional[float] = None,
    trajectory: Optional[Sequence[float]] = None,
) -> PinkNoiseResult:
    """Generate standardized finite 1/f noise through an rFFT pipeline.

    The low-frequency cutoff is clamped to the first non-zero FFT bin so the
    DC singularity cannot dominate the realization.  The returned entropy is
    computed from ``trajectory`` when supplied, otherwise from the generated
    realization itself.
    """
    array_lib = _require_numpy()
    if length < 4:
        raise ValueError("length must be at least 4 for an rFFT realization")
    if alpha < 0.0:
        raise ValueError("alpha must be non-negative")
    if sample_rate <= 0.0:
        raise ValueError("sample_rate must be positive")
    if low_frequency_hz <= 0.0:
        raise ValueError("low_frequency_hz must be positive")

    rng = array_lib.random.default_rng(seed)
    frequencies = array_lib.fft.rfftfreq(length, d=1.0 / sample_rate)
    first_bin = float(frequencies[1]) if frequencies.size > 1 else float(sample_rate / length)
    cutoff = max(float(low_frequency_hz), first_bin)
    safe_frequencies = array_lib.maximum(frequencies, cutoff)
    amplitude = safe_frequencies ** (-float(alpha) / 2.0)
    spectrum = (rng.standard_normal(frequencies.size) + 1j * rng.standard_normal(frequencies.size)) * amplitude
    spectrum[0] = 0.0
    if length % 2 == 0 and spectrum.size > 1:
        spectrum[-1] = complex(float(spectrum[-1].real), 0.0)

    samples = array_lib.fft.irfft(spectrum, n=length).astype(float)
    samples -= float(array_lib.mean(samples))
    standard_deviation = float(array_lib.std(samples))
    if standard_deviation <= 0.0 or not math.isfinite(standard_deviation):
        raise FloatingPointError("1/f realization could not be standardized")
    samples /= standard_deviation
    # The alpha estimate averages the periodogram over independent realizations
    # (Bartlett-style); a single periodogram's bins are chi-squared(2) noisy.
    power = (array_lib.abs(array_lib.fft.rfft(samples)) ** 2) / float(length)
    for _ in range(ALPHA_FIT_REALIZATIONS - 1):
        extra = (rng.standard_normal(frequencies.size) + 1j * rng.standard_normal(frequencies.size)) * amplitude
        extra[0] = 0.0
        extra_samples = array_lib.fft.irfft(extra, n=length).astype(float)
        extra_samples -= float(array_lib.mean(extra_samples))
        extra_std = float(array_lib.std(extra_samples))
        if extra_std > 0.0 and math.isfinite(extra_std):
            extra_samples /= extra_std
            power = power + (array_lib.abs(array_lib.fft.rfft(extra_samples)) ** 2) / float(length)
    power = power / float(ALPHA_FIT_REALIZATIONS)
    alpha_estimate = fit_one_over_f_alpha(
        frequencies, power, cutoff, high_frequency_hz=high_frequency_hz
    )
    entropy_source = samples if trajectory is None else trajectory
    return PinkNoiseResult(
        samples=samples,
        frequencies=frequencies,
        power_spectrum=power,
        alpha_target=float(alpha),
        alpha_estimate=alpha_estimate,
        entropy=shannon_entropy(entropy_source),
    )


# Friendly alias for callers using the shorter research name.
generate_pink_noise = generate_one_over_f_noise


def advance_stochastic_project_seth_step(
    x_k: float,
    eta_k: float,
    dt: float,
    sigma: float,
    A_k: float,
    C_k: float,
) -> float:
    """One exact Phase 3 Euler-Maruyama update.

    x[k+1] = x[k] + [A_k*(x[k] - x[k]^3) + C_k]*dt
                    + sigma*(1 + |x[k]|)*eta[k]*sqrt(dt)
    """
    if dt <= 0.0:
        raise ValueError("dt must be positive")
    if sigma < 0.0:
        raise ValueError("sigma must be non-negative")
    deterministic_drift = A_k * (x_k - x_k**3) + C_k
    diffusion_increment = sigma * (1.0 + abs(x_k)) * eta_k * math.sqrt(dt)
    return float(x_k + deterministic_drift * dt + diffusion_increment)


def simulate_project_seth_trajectory(
    initial_x: float,
    eta: Sequence[float],
    dt: float,
    sigma: float,
    A: float | Sequence[float],
    C: float | Sequence[float],
) -> Any:
    """Integrate a scalar Project Seth trajectory with shared forcing input."""
    steps = len(eta)
    if steps < 1:
        raise ValueError("eta must contain at least one forcing value")

    def value_at(value: float | Sequence[float], index: int) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if len(value) != steps:
            raise ValueError("A and C sequences must match eta length")
        return float(value[index])

    if np is None:
        trajectory: List[float] = [float(initial_x)]
        for index, eta_k in enumerate(eta):
            trajectory.append(
                advance_stochastic_project_seth_step(
                    trajectory[-1], float(eta_k), dt, sigma,
                    value_at(A, index), value_at(C, index),
                )
            )
        return trajectory

    eta_array = np.asarray(eta, dtype=float)
    trajectory_array = np.empty(steps + 1, dtype=float)
    trajectory_array[0] = float(initial_x)
    for index in range(steps):
        trajectory_array[index + 1] = advance_stochastic_project_seth_step(
            float(trajectory_array[index]),
            float(eta_array[index]),
            dt,
            sigma,
            value_at(A, index),
            value_at(C, index),
        )
    return trajectory_array


# Rolling per-endpoint observations from real HTTP attempts: (latency_seconds, ok).
ENDPOINT_TELEMETRY: Dict[str, Deque[Tuple[float, bool]]] = {}
TELEMETRY_WINDOW = 50
LATENCY_BASELINE_SECONDS = 2.0


def record_telemetry(endpoint_name: str, latency_seconds: float, ok: bool) -> None:
    bucket = ENDPOINT_TELEMETRY.setdefault(endpoint_name, deque(maxlen=TELEMETRY_WINDOW))
    bucket.append((max(0.0, float(latency_seconds)), bool(ok)))


def telemetry_snapshot(endpoint_name: str) -> Dict[str, float]:
    """Observed failure rate and latency ratio for an endpoint (zeros when unobserved)."""
    bucket = ENDPOINT_TELEMETRY.get(endpoint_name)
    if not bucket:
        return {"observations": 0.0, "failure_rate": 0.0, "latency_ratio": 0.0}
    failures = sum(1 for _, ok in bucket if not ok)
    successes = [latency for latency, ok in bucket if ok]
    mean_latency = (sum(successes) / len(successes)) if successes else LATENCY_BASELINE_SECONDS
    return {
        "observations": float(len(bucket)),
        "failure_rate": failures / len(bucket),
        "latency_ratio": max(0.0, mean_latency / LATENCY_BASELINE_SECONDS - 1.0),
    }


def _entropy_bins(sample_count: int) -> int:
    return max(8, min(128, int(math.sqrt(sample_count)) * 2))


PENALTY_WEIGHTS = {"failure_rate": 0.6, "latency": 0.3, "seth": 0.1}
_SETH_EDGES_RANGE = (-2.5, 2.5)


def project_seth_routing_entropy(
    endpoint_names: Iterable[str],
    seed: Optional[int] = None,
    length: int = 128,
    alpha: float = 1.0,
    telemetry: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> Dict[str, float]:
    """Observed-degradation penalty per endpoint, in [0, 1].

    penalty = 0.6 * failure_rate + 0.3 * min(1, latency_ratio / 2) + 0.1 * seth_term

    failure_rate and latency_ratio come from real HTTP attempts recorded by
    :func:`record_telemetry`; both terms are monotone in what was observed.
    seth_term is the Project Seth SDE driven by those same observables (bias C
    from the failure rate, noise gate sigma from latency), integrated on 1/f
    forcing seeded by the endpoint name so it is reproducible, and scored as
    the fixed-range histogram entropy gained over the undriven baseline. It is
    a bounded stochastic-shape component, never the majority of the penalty.
    An endpoint with no observations has no evidence against it and scores 0.
    This is a routing signal, not a physical claim.
    """
    names = list(endpoint_names)
    result: Dict[str, float] = {}
    for name in names:
        snapshot = dict((telemetry or {}).get(name) or telemetry_snapshot(name))
        if snapshot.get("observations", 0.0) <= 0.0:
            result[name] = 0.0
            continue
        failure_rate = max(0.0, min(1.0, float(snapshot.get("failure_rate", 0.0))))
        latency_ratio = max(0.0, min(3.0, float(snapshot.get("latency_ratio", 0.0))))
        seth_term = 0.0
        if np is not None:
            endpoint_seed = int(seed) if seed is not None else zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
            noise = generate_one_over_f_noise(
                length=length, alpha=alpha, seed=endpoint_seed, sample_rate=100.0, low_frequency_hz=max(0.01, 100.0 / length)
            )
            edges = np.linspace(_SETH_EDGES_RANGE[0], _SETH_EDGES_RANGE[1], _entropy_bins(length + 1) + 1)
            baseline = simulate_project_seth_trajectory(0.0, noise.samples, 0.01, 0.12, 0.55, 0.01)
            driven = simulate_project_seth_trajectory(
                0.0, noise.samples, 0.01, 0.12 * (1.0 + latency_ratio), 0.55, 0.01 + 0.30 * failure_rate
            )
            maximum = max(1.0, math.log2(len(edges) - 1))
            gain = (shannon_entropy(driven, bins=edges) - shannon_entropy(baseline, bins=edges)) / maximum
            seth_term = max(0.0, min(1.0, gain))
        penalty = (
            PENALTY_WEIGHTS["failure_rate"] * failure_rate
            + PENALTY_WEIGHTS["latency"] * min(1.0, latency_ratio / 2.0)
            + PENALTY_WEIGHTS["seth"] * seth_term
        )
        result[name] = max(0.0, min(1.0, penalty))
    return result


# =============================================================================
# Cortex 2 — deterministic binary controller
# =============================================================================

@dataclass
class MILPDecision:
    endpoint: CortexEndpoint
    decision_vector: Dict[str, int]
    utility_scores: Dict[str, float]
    entropy_penalties: Dict[str, float]
    solver: str
    reason: str


def _vendor(endpoint: "CortexEndpoint") -> str:
    """The ledger bucket for an endpoint: one bucket per credential/vendor."""
    return discovery.vendor_for(endpoint.name)


def _ensure_cortex_ledger(ledger: Optional[QuotaLedger]) -> None:
    """Register each Cortex endpoint's vendor bucket, tightening to the stricter policy.

    The legacy registry may already have registered the same vendor (for example
    ``gemini`` at 15 RPM); one key must be metered once, so the tighter of the
    two ceilings applies to both routing paths.
    """
    if ledger is None:
        return
    for endpoint in CORTEX_ENDPOINTS.values():
        ledger.tighten(
            _vendor(endpoint),
            endpoint.rpm_limit,
            endpoint.tpm_limit if endpoint.tpm_limit is not None else 10**9,
        )


LOCAL_ENDPOINT_NAME = "local"
LOCAL_ENV_URL = "CHAT_JOHNSON_LOCAL_ENDPOINT"
LOCAL_ENV_MODEL = "CHAT_JOHNSON_LOCAL_MODEL"
LOCAL_ENV_KEY = "CHAT_JOHNSON_LOCAL_KEY"
LOCAL_DEFAULT_MODEL = "llama3.1:8b"


def register_local_endpoint(base_url: str, model: str = LOCAL_DEFAULT_MODEL, rpm_limit: int = 60, tpm_limit: Optional[int] = None) -> CortexEndpoint:
    """Register a self-hosted OpenAI-compatible endpoint (Ollama, LM Studio, vLLM) as the ``local`` Cortex endpoint.

    Process-wide by design: on a self-hosted box the operator owns the process. The endpoint counts
    as keyed through ``CHAT_JOHNSON_LOCAL_KEY`` (any value; most local servers ignore it).
    """
    if not os.environ.get(LOCAL_ENV_KEY, "").strip():
        os.environ[LOCAL_ENV_KEY] = "local"
    endpoint = CortexEndpoint(
        name=LOCAL_ENDPOINT_NAME, label=f"Local model ({model})", env_keys=(LOCAL_ENV_KEY,), base_url=base_url.rstrip("/"),
        kind="openai", model=model or LOCAL_DEFAULT_MODEL, rpm_limit=int(rpm_limit), tpm_limit=tpm_limit, speed_score=0.35,
        context_score=0.45, strengths=("quick_text", "chat"), model_env=LOCAL_ENV_MODEL,
    )
    CORTEX_ENDPOINTS[LOCAL_ENDPOINT_NAME] = endpoint
    return endpoint


def unregister_local_endpoint() -> None:
    CORTEX_ENDPOINTS.pop(LOCAL_ENDPOINT_NAME, None)


def local_endpoint() -> Optional[CortexEndpoint]:
    return CORTEX_ENDPOINTS.get(LOCAL_ENDPOINT_NAME)


def register_local_endpoint_from_env() -> Optional[CortexEndpoint]:
    """``CHAT_JOHNSON_LOCAL_ENDPOINT`` (and optional ``CHAT_JOHNSON_LOCAL_MODEL``) register the local endpoint at start-up."""
    url = os.environ.get(LOCAL_ENV_URL, "").strip()
    if not url:
        return None
    return register_local_endpoint(url, os.environ.get(LOCAL_ENV_MODEL, "").strip() or LOCAL_DEFAULT_MODEL)


def local_first_generate(
    mode: str, task_type: str, messages: List[dict], ledger: QuotaLedger, max_tokens: int = 1024, temperature: float = 0.3,
) -> Tuple[str, "RouteDecision"]:
    """Cheap labour goes to the local model when one is registered; otherwise (or on failure) the normal router."""
    endpoint = local_endpoint()
    if endpoint is not None and _endpoint_key(endpoint):
        status: Dict[str, Any] = {}
        try:
            text = "".join(cortex_stream(endpoint, messages, max_tokens=max_tokens, temperature=temperature, ledger=ledger, status=status))
            if ledger is not None:
                ledger.record(_vendor(endpoint), _estimate_tokens(messages, text), count_request=False)
            return text, RouteDecision(endpoint.name, endpoint_model(endpoint), task_type, "tier routing: local model first", finish=str(status.get("finish", "")))
        except ProviderError:
            pass  # the local box is down or overloaded: cloud free tiers take the call
    return generate_mode(mode, task_type, messages, ledger, max_tokens=max_tokens, temperature=temperature)


def _endpoint_key(endpoint: CortexEndpoint) -> str:
    for env_name in endpoint.env_keys:
        value = resolve_secret(env_name)
        if value:
            return value
    return ""


DEFAULT_CONTEXT_CHARS = 24_000
MIN_CONTEXT_CHARS = 8_000
CONTEXT_RESERVE_TOKENS = 500


def prompt_context_chars(max_tokens: int) -> int:
    """Characters of project memory that keep a request inside every keyed endpoint's TPM ceiling.

    4 chars per token as in _estimate_tokens, a 500-token reserve for the system prompt and
    the request itself, never below 8k (context matters more than one fast endpoint) and never
    above the 24k default. At the default 2,048-token budget this keeps Groq's 8,000 TPM feasible.
    """
    cap = DEFAULT_CONTEXT_CHARS
    for endpoint in CORTEX_ENDPOINTS.values():
        if endpoint.tpm_limit is None or not _endpoint_key(endpoint):
            continue
        cap = min(cap, 4 * (int(endpoint.tpm_limit) - int(max_tokens) - CONTEXT_RESERVE_TOKENS))
    return max(MIN_CONTEXT_CHARS, min(DEFAULT_CONTEXT_CHARS, cap))


REPOSITORY_CONTEXT_MIN_CHARS = 4_000
REPOSITORY_CONTEXT_MAX_CHARS = 60_000
REPOSITORY_CONTEXT_RESERVE_TOKENS = 1_500


def repository_context_chars(max_tokens: int) -> int:
    """Characters of repository context the widest keyed endpoint can take alongside the output budget.

    The solver already steers an oversized request away from a narrow endpoint (Groq's 8k TPM), so
    this follows the widest window rather than the narrowest; 24k when no endpoint is keyed.
    """
    widest = 0
    for endpoint in CORTEX_ENDPOINTS.values():
        if endpoint.tpm_limit is None or not _endpoint_key(endpoint):
            continue
        widest = max(widest, 4 * (int(endpoint.tpm_limit) - int(max_tokens) - REPOSITORY_CONTEXT_RESERVE_TOKENS))
    if widest <= 0:
        return DEFAULT_CONTEXT_CHARS if not any(_endpoint_key(e) for e in CORTEX_ENDPOINTS.values()) else REPOSITORY_CONTEXT_MIN_CHARS
    return max(REPOSITORY_CONTEXT_MIN_CHARS, min(REPOSITORY_CONTEXT_MAX_CHARS, widest))


def cortex_wait_seconds(ledger: Optional[QuotaLedger], messages: Sequence[Mapping[str, str]], max_tokens: int) -> float:
    """Seconds until some keyed Cortex endpoint has RPM/TPM headroom for this request; 0 when one has it now."""
    if ledger is None:
        return 0.0
    _ensure_cortex_ledger(ledger)
    estimated = _estimate_tokens(messages) + int(max_tokens)
    waits = [ledger.wait_seconds(_vendor(endpoint), estimated) for endpoint in CORTEX_ENDPOINTS.values() if _endpoint_key(endpoint)]
    return float(min(waits)) if waits else 0.0


def _endpoint_usage(
    endpoint: CortexEndpoint,
    ledger: Optional[QuotaLedger],
    current_usage: Optional[Mapping[str, Mapping[str, float]]],
) -> Tuple[float, float]:
    for key in (endpoint.name, _vendor(endpoint)):
        if current_usage and key in current_usage:
            row = current_usage[key]
            return float(row.get("rpm_used", 0.0)), float(row.get("tpm_used", 0.0))
    if ledger is not None:
        try:
            row = ledger.usage(_vendor(endpoint))
            return float(row.get("rpm_used", 0.0)), float(row.get("tpm_used", 0.0))
        except KeyError:
            return 0.0, 0.0
    return 0.0, 0.0


def _utility_score(endpoint: CortexEndpoint, task_type: str, entropy_penalty: float) -> float:
    context_weight = {
        "context_load": 0.92,
        "reasoning": 0.72,
        "test_fix": 0.56,
        "code_patch": 0.30,
        "quick_text": 0.12,
        "chat": 0.35,
    }.get(task_type, 0.35)
    raw = (1.0 - context_weight) * endpoint.speed_score + context_weight * endpoint.context_score
    task_bonus = 0.10 if task_type in endpoint.strengths else 0.0
    # Higher channel entropy is a bounded penalty, not a scientific claim that
    # the random realization measures provider quality.
    return float(raw + task_bonus - 0.30 * max(0.0, min(1.0, entropy_penalty)))


def _feasible_endpoint(endpoint: CortexEndpoint, rpm_used: float, tpm_used: float, estimated_tokens: int) -> bool:
    """Kept for callers/tests: the same capacity rows the solver enforces, evaluated in Python."""
    return bool(_endpoint_key(endpoint)) and not _capacity_reasons(endpoint, rpm_used, tpm_used, estimated_tokens)


def _capacity_reasons(endpoint: CortexEndpoint, rpm_used: float, tpm_used: float, estimated_tokens: int) -> List[str]:
    """Human-readable reasons an endpoint cannot take this request right now."""
    reasons: List[str] = []
    if rpm_used + 1.0 > endpoint.rpm_limit:
        reasons.append(f"{int(rpm_used)}/{endpoint.rpm_limit} requests used in the last minute")
    if endpoint.tpm_limit is not None:
        if estimated_tokens > endpoint.tpm_limit:
            reasons.append(
                f"request needs ~{estimated_tokens} tokens but the ceiling is {endpoint.tpm_limit} TPM "
                "(lower the output token budget or shorten the prompt)"
            )
        elif tpm_used + estimated_tokens > endpoint.tpm_limit:
            reasons.append(f"~{int(tpm_used)}+{estimated_tokens} tokens would exceed {endpoint.tpm_limit} TPM this minute")
    return reasons


def _build_constraint_array(
    endpoints: Sequence[CortexEndpoint],
    usage: Mapping[str, Tuple[float, float]],
    estimated_tokens: int,
    enabled: Mapping[str, bool],
) -> Any:
    """Constraint rows for scipy.milp: exclusivity, RPM capacity, TPM capacity, key/exclusion.

    These rows decide feasibility. ``enabled`` only encodes "has a key and is not
    excluded"; the RPM/TPM ceilings are enforced here, by the solver.
    """
    if np is None or LinearConstraint is None:
        return None
    array_lib = _require_numpy()

    rows: List[Any] = [array_lib.ones(len(endpoints), dtype=float)]
    lower: List[float] = [1.0]
    upper: List[float] = [1.0]

    for index, endpoint in enumerate(endpoints):
        rpm_used, tpm_used = usage[endpoint.name]
        row = array_lib.zeros(len(endpoints), dtype=float)
        row[index] = 1.0
        rows.append(row)
        lower.append(-array_lib.inf)
        upper.append(float(max(0.0, endpoint.rpm_limit - rpm_used)))  # x_i <= remaining requests
        if endpoint.tpm_limit is not None:
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = float(estimated_tokens)
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(float(max(0.0, endpoint.tpm_limit - tpm_used)))  # tokens * x_i <= remaining tokens
        if not enabled.get(endpoint.name, False):
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = 1.0
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(0.0)  # no key / excluded: x_i <= 0
    return LinearConstraint(array_lib.asarray(rows), array_lib.asarray(lower), array_lib.asarray(upper))


def select_milp_endpoint(
    task_type: str,
    estimated_tokens: int,
    ledger: Optional[QuotaLedger] = None,
    entropy_by_endpoint: Optional[Mapping[str, float]] = None,
    current_usage: Optional[Mapping[str, Mapping[str, float]]] = None,
    excluded: Optional[Iterable[str]] = None,
) -> MILPDecision:
    """Select exactly one BYOK endpoint under strict RPM/TPM constraints.

    SciPy's ``milp`` receives binary integrality and a LinearConstraint whose
    rows enforce sum(x)=1, per-endpoint RPM and TPM capacity, and key/exclusion.
    The solver decides feasibility; when SciPy is absent the identical rows are
    evaluated in Python. When nothing is feasible the error names, per endpoint,
    exactly which ceiling blocks it.
    """
    if estimated_tokens < 1:
        raise ValueError("estimated_tokens must be at least one")
    _ensure_cortex_ledger(ledger)
    excluded_set = set(excluded or ())
    endpoints = list(CORTEX_ENDPOINTS.values())
    usage = {endpoint.name: _endpoint_usage(endpoint, ledger, current_usage) for endpoint in endpoints}
    supplied_entropy = dict(entropy_by_endpoint or {})
    if not supplied_entropy:
        supplied_entropy = project_seth_routing_entropy((endpoint.name for endpoint in endpoints), length=128)
    penalties = {endpoint.name: float(supplied_entropy.get(endpoint.name, 0.0)) for endpoint in endpoints}
    utilities = {endpoint.name: _utility_score(endpoint, task_type, penalties[endpoint.name]) for endpoint in endpoints}
    enabled = {
        endpoint.name: endpoint.name not in excluded_set and bool(_endpoint_key(endpoint)) for endpoint in endpoints
    }
    # The same capacity rows the solver sees, evaluated for the fallback and for diagnostics.
    blocked: Dict[str, List[str]] = {}
    for endpoint in endpoints:
        if not enabled[endpoint.name]:
            blocked[endpoint.name] = ["no key configured" if not _endpoint_key(endpoint) else "already tried this request"]
            continue
        reasons = _capacity_reasons(endpoint, usage[endpoint.name][0], usage[endpoint.name][1], estimated_tokens)
        if reasons:
            blocked[endpoint.name] = reasons
    feasible = [endpoint for endpoint in endpoints if endpoint.name not in blocked]

    decision_vector: Dict[str, int]
    solver_name = "deterministic-binary-fallback"
    chosen: Optional[CortexEndpoint] = None
    constraint = _build_constraint_array(endpoints, usage, estimated_tokens, enabled)

    if milp is not None and constraint is not None and Bounds is not None:
        objective = -np.asarray([utilities[endpoint.name] for endpoint in endpoints], dtype=float)
        result = milp(
            c=objective,
            integrality=np.ones(len(endpoints), dtype=int),
            bounds=Bounds(np.zeros(len(endpoints)), np.ones(len(endpoints))),
            constraints=[constraint],
            options={"presolve": True},
        )
        if bool(result.success) and result.x is not None:
            selected_indices = [index for index, value in enumerate(result.x) if value >= 0.5]
            if len(selected_indices) == 1:
                chosen = endpoints[selected_indices[0]]
                solver_name = "scipy.optimize.milp"
        elif feasible:
            # Solver and Python rows disagree only if the model is malformed; surface it loudly.
            raise ProviderError(f"Cortex 2 solver reported infeasible while rows allow {[e.name for e in feasible]}")

    if chosen is None:
        if not feasible:
            detail = "; ".join(f"{name}: {', '.join(reasons)}" for name, reasons in blocked.items())
            raise ProviderError(f"Cortex 2 found no BYOK endpoint with headroom -> {detail}")
        chosen = max(feasible, key=lambda endpoint: (utilities[endpoint.name], -endpoints.index(endpoint)))

    decision_vector = {endpoint.name: int(endpoint.name == chosen.name) for endpoint in endpoints}
    return MILPDecision(
        endpoint=chosen,
        decision_vector=decision_vector,
        utility_scores=utilities,
        entropy_penalties=penalties,
        solver=solver_name,
        reason=(
            f"selected {chosen.name}; utility={utilities[chosen.name]:.4f}; "
            f"entropy_penalty={penalties[chosen.name]:.4f}; binary={decision_vector}"
        ),
    )


# =============================================================================
# Cortex 1 — HTTP payloads and generation streams
# =============================================================================

Message = Dict[str, str]


def _as_endpoint(value: str | CortexEndpoint | MILPDecision) -> CortexEndpoint:
    if isinstance(value, CortexEndpoint):
        return value
    if isinstance(value, MILPDecision):
        return value.endpoint
    try:
        return CORTEX_ENDPOINTS[value]
    except KeyError as exc:
        raise ProviderError(f"unknown Cortex endpoint: {value}") from exc


def append_system_prompt(messages: Sequence[Mapping[str, str]], system_prompt: str = "") -> List[Message]:
    """Copy messages and append a single explicit system instruction."""
    normalized = [
        {"role": str(message.get("role", "user")), "content": str(message.get("content", ""))}
        for message in messages
    ]
    instruction = system_prompt.strip()
    if not instruction:
        return normalized
    system_indices = [index for index, message in enumerate(normalized) if message["role"] == "system"]
    if system_indices:
        first = system_indices[0]
        normalized[first]["content"] = normalized[first]["content"].rstrip() + "\n\n" + instruction
        return normalized
    return [{"role": "system", "content": instruction}, *normalized]


def list_endpoint_models(endpoint: str | CortexEndpoint, timeout: int = 20) -> List[str]:
    """Return generation-capable model ids from the vendor's model list (needs a key)."""
    selected = _as_endpoint(endpoint)
    return discovery.list_models(selected.kind, selected.base_url, _endpoint_key(selected), timeout=timeout)


def _model_is_usable(selected: CortexEndpoint, model_id: str, timeout: int = 20, ledger: Optional[QuotaLedger] = None) -> bool:
    """One-token, non-streaming call: True on 2xx or a transient status, False if this key cannot use the model."""
    if requests is None:
        return False
    try:
        url, headers, payload = build_cortex_request(
            selected, [{"role": "user", "content": "ping"}], max_tokens=8, temperature=0.0, stream=False, model_id=model_id
        )
        if ledger is not None:
            ledger.record_attempt(_vendor(selected))
        started = time.monotonic()
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        record_telemetry(selected.name, time.monotonic() - started, int(response.status_code) < 400)
    except (ProviderError, requests.RequestException):
        return False
    status = int(response.status_code)
    body = _providers.body_text(response)
    try:
        response.close()
    except AttributeError:
        pass
    if status < 400:
        return True
    if discovery.is_transient(status):
        return True  # exists and is permitted, merely busy right now
    return not discovery.looks_like_unusable_model(status, body) and status < 500


def discover_endpoint_model(
    endpoint: str | CortexEndpoint,
    timeout: int = 20,
    exclude: Tuple[str, ...] = (),
    ledger: Optional[QuotaLedger] = None,
    max_candidates: int = discovery.MAX_VALIDATION_CANDIDATES,
) -> Optional[str]:
    """Pick a live, key-usable replacement id from the vendor list and cache it for this process."""
    selected = _as_endpoint(endpoint)
    return discovery.discover(
        selected.name,
        selected.kind,
        selected.base_url,
        _endpoint_key(selected),
        timeout=timeout,
        exclude=exclude,
        validate=lambda candidate: _model_is_usable(selected, candidate, timeout=min(timeout, 20), ledger=ledger),
        max_candidates=max_candidates,
    )


def _resilient_post(
    selected: CortexEndpoint,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
    temperature: float,
    stream: bool,
    system_prompt: str,
    timeout: int,
    ledger: Optional[QuotaLedger] = None,
    probe: bool = False,
) -> Tuple[Any, str]:
    """POST to the endpoint with retirement recovery, transient retries, and sibling fallback.

    Returns ``(response, model_id)`` with a 2xx response, or raises ProviderError
    carrying the last real status. Order of operations per model:
    up to MAX_TRANSIENT_ATTEMPTS tries with backoff on 429/5xx, one rediscovery
    on a retired-id error, then up to two sibling models if still overloaded.
    """
    secret = _endpoint_key(selected)
    key_override = os.environ.get(selected.model_env, "").strip() if selected.model_env else ""
    tried: List[str] = []
    model_id = endpoint_model(selected)
    rediscovered = False
    last_error = "no attempt made"
    max_attempts = 1 if probe else discovery.MAX_TRANSIENT_ATTEMPTS
    max_models = 2 if probe else 4  # a probe may follow one retired-id rediscovery, never sibling fallback
    vendor = _vendor(selected)

    while True:
        tried.append(model_id)
        for attempt in range(1, max_attempts + 1):
            url, headers, payload = build_cortex_request(
                selected, messages, max_tokens, temperature, stream=stream, system_prompt=system_prompt, model_id=model_id
            )
            if ledger is not None:
                ledger.record_attempt(vendor)  # every real POST counts toward the vendor's RPM
            started = time.monotonic()
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=timeout, stream=stream)
            except requests.RequestException as exc:
                record_telemetry(selected.name, time.monotonic() - started, False)
                last_error = f"network error: {exc}"
                if attempt < max_attempts:
                    discovery.sleep(discovery.retry_delay(attempt))
                    continue
                raise ProviderError(f"{selected.name} {last_error}") from exc
            latency = time.monotonic() - started
            if response.status_code < 400:
                record_telemetry(selected.name, latency, True)
                return response, model_id
            record_telemetry(selected.name, latency, False)
            detail = _providers.body_text(response)
            retry_after = response.headers.get("Retry-After") if hasattr(response, "headers") else None
            response.close()
            if secret:
                detail = detail.replace(secret, "[REDACTED_SECRET]")
            last_error = f"HTTP {response.status_code}: {detail}"
            if discovery.is_transient(response.status_code) and attempt < max_attempts:
                discovery.sleep(discovery.retry_delay(attempt, retry_after))
                continue
            break  # permanent error, or transient retries exhausted, for this model

        status = int(response.status_code)
        original = len(tried) == 1
        if not key_override and not rediscovered and discovery.looks_like_retired_model(status, last_error):
            rediscovered = True
            replacement = discover_endpoint_model(
                selected, timeout=min(timeout, 20), exclude=tuple(tried), ledger=ledger, max_candidates=1 if probe else discovery.MAX_VALIDATION_CANDIDATES
            )
            if replacement and replacement not in tried:
                model_id = replacement
                continue
        if not key_override and not original and len(tried) < max_models and discovery.looks_like_unusable_model(status, last_error):
            # A discovered or sibling id this key cannot use (OAuth-only, allowlisted, gone):
            # drop it from the cache and move to the next validated candidate.
            discovery.DISCOVERED.pop(discovery.vendor_for(selected.name), None)
            replacement = discover_endpoint_model(selected, timeout=min(timeout, 20), exclude=tuple(tried), ledger=ledger)
            if replacement and replacement not in tried:
                model_id = replacement
                continue
        if not key_override and not probe and discovery.is_transient(status):
            siblings = [
                name for name in discovery.alternates(
                    selected.name, selected.kind, selected.base_url, secret, model_id, timeout=min(timeout, 20)
                )
                if name not in tried
            ]
            if siblings:
                model_id = siblings[0]
                continue
        raise ProviderError(f"{selected.name} {last_error} (models tried: {tried})")


def build_cortex_request(
    endpoint: str | CortexEndpoint,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
    temperature: float,
    stream: bool,
    system_prompt: str = "",
    model_id: Optional[str] = None,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Format a provider-specific URL, redacted-safe headers, and JSON body."""
    selected = _as_endpoint(endpoint)
    prepared = append_system_prompt(messages, system_prompt)
    key = _endpoint_key(selected)
    if not key:
        raise ProviderError(f"no BYOK key configured for {selected.name}")
    resolved_model = model_id or endpoint_model(selected)

    if selected.kind == "gemini":
        system_parts = [message["content"] for message in prepared if message["role"] == "system"]
        contents = [
            {
                "role": "model" if message["role"] == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
            for message in prepared
            if message["role"] in ("user", "assistant")
        ]
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": int(max_tokens),
                "temperature": float(temperature),
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        url = (
            f"{selected.base_url}/models/{resolved_model}:streamGenerateContent?alt=sse"
            if stream
            else f"{selected.base_url}/models/{resolved_model}:generateContent"
        )
        return url, {"x-goog-api-key": key, "Content-Type": "application/json"}, payload

    payload = {
        "model": resolved_model,
        "messages": prepared,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": bool(stream),
    }
    return (
        f"{selected.base_url}/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        payload,
    )


def _extract_finish(selected: CortexEndpoint, payload: Mapping[str, Any]) -> str:
    """Normalize the vendor's finish reason: 'length' for an output-budget cut, 'filtered' for a safety cut."""
    if selected.kind == "gemini":
        candidates = payload.get("candidates", [])
        raw = str(candidates[0].get("finishReason") or "") if candidates else ""
    else:
        choices = payload.get("choices", [])
        raw = str(choices[0].get("finish_reason") or "") if choices else ""
    raw = raw.strip().lower()
    if raw in ("length", "max_tokens"):
        return "length"
    if raw in ("content_filter", "safety", "recitation", "blocklist", "prohibited_content", "spii"):
        return "filtered"
    return raw


def _extract_stream_text(selected: CortexEndpoint, payload: Mapping[str, Any]) -> str:
    if selected.kind == "gemini":
        candidates = payload.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(str(part.get("text", "")) for part in parts)
    choices = payload.get("choices", [])
    if not choices:
        return ""
    choice = choices[0]
    delta = choice.get("delta", {})
    return str(delta.get("content", choice.get("message", {}).get("content", "")) or "")


def _iter_sse_payloads(response: requests.Response) -> Iterator[Mapping[str, Any]]:
    # Vendors send text/event-stream without a charset; requests would then decode
    # as ISO-8859-1 and turn UTF-8 punctuation into mojibake. Decode bytes ourselves.
    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        data = line[5:].strip() if line.startswith("data:") else line
        if data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            if "error" in parsed and not parsed.get("choices") and not parsed.get("candidates"):
                error = parsed.get("error")
                message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise ProviderError(f"stream error frame: {str(message)[:300]}")
            yield parsed


def cortex_stream(
    endpoint: str | CortexEndpoint,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
    timeout: Optional[int] = None,
    ledger: Optional[QuotaLedger] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Iterator[str]:
    """Yield generated text chunks from a MILP-selected provider endpoint.

    ``status`` (when given) receives ``finish`` once the vendor reports why the answer ended.

    Transient vendor errors are retried with backoff, a retired model id is
    replaced from the vendor's live list, and an overloaded model falls back
    to a sibling on the same vendor before the endpoint is declared failed.
    """
    if requests is None:
        raise ProviderError("requests is required for provider HTTP execution")
    selected = _as_endpoint(endpoint)
    effective_timeout = timeout or int(os.environ.get("CHAT_JOHNSON_TIMEOUT", "120"))
    response, _ = _resilient_post(
        selected, messages, max_tokens, temperature, stream=True, system_prompt=system_prompt,
        timeout=effective_timeout, ledger=ledger,
    )
    emitted = False
    try:
        for payload_item in _iter_sse_payloads(response):
            finish = _extract_finish(selected, payload_item)
            if finish and status is not None:
                status["finish"] = finish
            chunk = _extract_stream_text(selected, payload_item)
            if chunk:
                emitted = True
                yield chunk
    finally:
        response.close()
    if not emitted:
        raise ProviderError(f"{selected.name} returned an empty stream (no text; blocked, truncated, or filtered)")


_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"^\s*<think>.*\Z", re.DOTALL | re.IGNORECASE)


def strip_reasoning_tags(text: str) -> str:
    """Remove <think>…</think> blocks reasoning models put in message content; never show hidden reasoning."""
    cleaned = _THINK_BLOCK.sub("", text or "")
    cleaned = _THINK_OPEN.sub("", cleaned)  # an unterminated block means the answer never started
    return cleaned.strip() if cleaned != text else text


_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


def _partial_tag_suffix(text: str, tag: str) -> int:
    """Length of the longest suffix of ``text`` that is a proper prefix of ``tag`` (case-insensitive)."""
    lowered = text.lower()
    for length in range(min(len(tag) - 1, len(lowered)), 0, -1):
        if lowered.endswith(tag[:length]):
            return length
    return 0


def _visible_chunks(chunks: Iterable[str]) -> Iterator[str]:
    """Stream only the text outside <think>…</think>; a tag split across chunks is held back until it resolves."""
    buffer = ""
    hidden = False
    for chunk in chunks:
        buffer += chunk
        while buffer:
            lowered = buffer.lower()
            if hidden:
                end = lowered.find(_THINK_CLOSE_TAG)
                if end < 0:
                    keep = _partial_tag_suffix(buffer, _THINK_CLOSE_TAG)
                    buffer = buffer[len(buffer) - keep:] if keep else ""
                    break
                buffer = buffer[end + len(_THINK_CLOSE_TAG):].lstrip()
                hidden = False
                continue
            start = lowered.find(_THINK_OPEN_TAG)
            if start < 0:
                safe = len(buffer) - _partial_tag_suffix(buffer, _THINK_OPEN_TAG)
                if safe:
                    yield buffer[:safe]
                buffer = buffer[safe:]
                break
            if start:
                yield buffer[:start]
            buffer = buffer[start + len(_THINK_OPEN_TAG):]
            hidden = True
    if buffer and not hidden:
        yield buffer


def _estimate_tokens(messages: Sequence[Mapping[str, str]], output: str = "") -> int:
    characters = sum(len(str(message.get("content", ""))) for message in messages) + len(output)
    return max(1, characters // 4)


def cortex_generate(
    task_type: str,
    messages: Sequence[Mapping[str, str]],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
) -> Tuple[str, "RouteDecision"]:
    """Run Cortex 2 selection, then consume the Cortex 1 generation stream.

    Every endpoint failure is retained so the final error names each provider
    and its real HTTP status instead of a generic "no headroom" message.
    """
    estimated_tokens = _estimate_tokens(messages) + max_tokens
    excluded: set[str] = set()
    attempted: List[str] = []
    failures: Dict[str, str] = {}
    for _ in range(len(CORTEX_ENDPOINTS)):
        try:
            decision = select_milp_endpoint(
                task_type,
                estimated_tokens,
                ledger=ledger,
                excluded=excluded,
            )
        except ProviderError as selection_error:
            if failures:
                break  # every keyed endpoint was already tried; report those errors below
            raise selection_error
        endpoint = decision.endpoint
        attempted.append(endpoint.name)
        status: Dict[str, Any] = {}
        try:
            chunks = cortex_stream(
                endpoint,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                system_prompt=system_prompt,
                ledger=ledger,
                status=status,
            )
            text = strip_reasoning_tags("".join(chunks))
            tokens = _estimate_tokens(messages, text)
            if ledger is not None:
                ledger.record(_vendor(endpoint), tokens, count_request=False)
            route_decision = RouteDecision(
                finish=str(status.get("finish", "")),
                provider=endpoint.name,
                model=endpoint_model(endpoint),
                task_type=task_type,
                reason=f"{decision.reason}; solver={decision.solver}; attempted={attempted}",
                solver=decision.solver,
                decision_vector=decision.decision_vector,
            )
            return text, route_decision
        except ProviderError as exc:
            excluded.add(endpoint.name)
            failures[endpoint.name] = str(exc)

    detail = "; ".join(f"{name}: {error}" for name, error in failures.items()) or "no keyed endpoint"
    raise ProviderError(f"all Cortex endpoints failed -> {detail}")


class CortexStream:
    """Iterable live stream that also records the routing decision and final text.

    Cortex 2 selects the endpoint when the object is created, so ``decision``
    is available before the first chunk.  ``text`` accumulates every chunk and
    the ledger is charged once the stream is exhausted.
    """

    def __init__(
        self,
        task_type: str,
        messages: Sequence[Mapping[str, str]],
        ledger: Optional[QuotaLedger] = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        system_prompt: str = "",
    ) -> None:
        self.messages = [dict(message) for message in messages]
        self.ledger = ledger
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system_prompt = system_prompt
        estimated_tokens = _estimate_tokens(messages) + max_tokens
        self.milp = select_milp_endpoint(task_type, estimated_tokens, ledger=ledger)
        self.decision = RouteDecision(
            provider=self.milp.endpoint.name,
            model=endpoint_model(self.milp.endpoint),
            task_type=task_type,
            reason=f"{self.milp.reason}; solver={self.milp.solver}; streamed=True",
            solver=self.milp.solver,
            decision_vector=self.milp.decision_vector,
        )
        self.text = ""
        self.status: Dict[str, Any] = {}

    def _raw(self) -> Iterator[str]:
        for chunk in cortex_stream(
            self.milp.endpoint,
            self.messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system_prompt=self.system_prompt,
            ledger=self.ledger,
            status=self.status,
        ):
            self.text += chunk
            yield chunk

    def __iter__(self) -> Iterator[str]:
        # Hidden reasoning is filtered live, not only after the stream ends.
        yield from _visible_chunks(self._raw())
        self.text = strip_reasoning_tags(self.text)
        self.decision.finish = str(self.status.get("finish", ""))
        if self.ledger is not None:
            self.ledger.record(_vendor(self.milp.endpoint), _estimate_tokens(self.messages, self.text), count_request=False)


def stream_generation(
    task_type: str,
    messages: Sequence[Mapping[str, str]],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
) -> Iterator[str]:
    """Select one endpoint with MILP and expose its live text stream."""
    return iter(CortexStream(task_type, messages, ledger, max_tokens, temperature, system_prompt))


# =============================================================================
# Connection probe (for the sidebar "Test keys" button)
# =============================================================================

def key_fingerprint(secret: str) -> str:
    """Non-reversible hint that lets an operator recognise which key is in use."""
    cleaned = (secret or "").strip()
    if not cleaned:
        return "none"
    prefix = cleaned[:4] if len(cleaned) > 12 else cleaned[:2]
    return f"{prefix}…({len(cleaned)} chars)"


def probe_endpoint(endpoint: str | CortexEndpoint, timeout: int = 20, ledger: Optional[QuotaLedger] = None) -> Dict[str, Any]:
    """Send a one-token request to an endpoint and report the real HTTP outcome.

    Returns a redacted dict: {"endpoint", "model", "ok", "status", "detail", "key"}.
    ``key`` is only a fingerprint (first few characters + length); the secret
    itself never appears in the result.
    """
    selected = _as_endpoint(endpoint)
    result: Dict[str, Any] = {
        "endpoint": selected.name,
        "model": endpoint_model(selected),
        "ok": False,
        "status": None,
        "detail": "",
        "key": key_fingerprint(_endpoint_key(selected)),
    }
    if requests is None:
        result["detail"] = "requests library missing"
        return result
    if not _endpoint_key(selected):
        result["detail"] = "no key configured"
        return result
    configured = endpoint_model(selected)
    try:
        response, used_model = _resilient_post(
            selected, [{"role": "user", "content": "ping"}], 8, 0.0, stream=False, system_prompt="",
            timeout=timeout, ledger=ledger, probe=True,
        )
    except ProviderError as exc:
        text = str(exc)
        match = re.search(r"HTTP (\d{3})", text)
        result["status"] = int(match.group(1)) if match else None
        if result["status"] in (401, 403):
            result["detail"] = f"key rejected: {text}"
        elif result["status"] == 404:
            result["detail"] = f"model id not found (override with {selected.model_env}): {text}"
        elif result["status"] == 429:
            result["detail"] = f"rate limited: {text}"
        else:
            result["detail"] = text
        return result
    healed = f" (auto-switched from {configured} to {used_model})" if used_model != configured else ""
    result["model"] = used_model
    response.close()
    result["status"] = int(response.status_code)
    result["ok"] = True
    result["detail"] = "reachable" + healed
    return result


def probe_legacy_provider(name: str, timeout: int = 20, ledger: Optional[QuotaLedger] = None) -> Dict[str, Any]:
    """One-token call through the legacy client for a configured non-Cortex provider."""
    from .config import provider_api_key  # local import: keep module import order stable

    cfg = PROVIDERS[name]
    key = provider_api_key(cfg)
    result: Dict[str, Any] = {
        "endpoint": name, "model": provider_model(cfg), "ok": False, "status": None,
        "detail": "no key configured", "key": key_fingerprint(key),
    }
    if not key:
        return result
    settings = get_settings()
    settings.request_timeout = timeout
    settings.max_retries_per_call = 1
    vendor = discovery.vendor_for(name)
    hook = (lambda: ledger.record_attempt(vendor)) if ledger is not None else None
    try:
        with _providers.metered(hook):
            chat(name, [{"role": "user", "content": "ping"}], 8, 0.0, settings)
    except ProviderError as exc:
        text = str(exc).replace(key, "[REDACTED_SECRET]")
        status = getattr(exc, "status_code", None)
        if status is None:
            match = re.search(r"HTTP (\d{3})", text)
            status = int(match.group(1)) if match else None
        result["status"] = status
        if status in (401, 403):
            result["detail"] = f"key rejected: {text}"
        elif status == 404:
            result["detail"] = f"model id not found (override with {cfg.model_env}): {text}"
        elif status == 429:
            result["detail"] = f"rate limited: {text}"
        else:
            result["detail"] = text
        return result
    except Exception as exc:  # network / parsing
        result["detail"] = f"error: {type(exc).__name__}"
        return result
    used = provider_model(cfg)
    result.update({"ok": True, "status": 200, "model": used,
                   "detail": "reachable" + (f" (auto-switched to {used})" if used != cfg.default_model and not os.environ.get(cfg.model_env, "").strip() else "")})
    return result


LEGACY_ONLY_PROVIDERS = ("nvidia", "openrouter", "cerebras", "mistral")


def probe_all_endpoints(timeout: int = 20, ledger: Optional[QuotaLedger] = None) -> List[Dict[str, Any]]:
    """Probe the strict Cortex endpoints, then every legacy provider that has a key. Every call is metered."""
    if ledger is not None:
        _ensure_cortex_ledger(ledger)
    rows = [probe_endpoint(endpoint, timeout=timeout, ledger=ledger) for endpoint in CORTEX_ENDPOINTS.values()]
    for name in LEGACY_ONLY_PROVIDERS:
        if name in PROVIDERS:
            rows.append(probe_legacy_provider(name, timeout=timeout, ledger=ledger))
    return rows


# =============================================================================
# Optional paid reasoning slot (Heavy Mode critique pass only)
# =============================================================================

PAID_SLOT_DEFAULT_BASE_URL = "https://api.openai.com/v1"
PAID_SLOT_DEFAULT_MODEL = "o3-mini"


@dataclass
class PaidReasoningSlot:
    """A per-session paid endpoint for the Heavy Mode critique pass.

    Policy (operator decision, 2026-09-13): the backend is free-tier only.  This
    slot is the single sanctioned exception and it is deliberately awkward to
    keep on: the key lives only in this object (never in the environment, the
    vault, or logs), it must be re-entered every session, and ``enabled`` must
    be set explicitly each time.  Nothing paid is reachable by default.
    """

    api_key: str = ""
    model: str = PAID_SLOT_DEFAULT_MODEL
    base_url: str = PAID_SLOT_DEFAULT_BASE_URL
    enabled: bool = False

    @property
    def armed(self) -> bool:
        return bool(self.enabled and self.api_key.strip() and self.model.strip())

    def status(self) -> Dict[str, Any]:
        """Redacted view for UI display; never includes the key."""
        return {"enabled": bool(self.enabled), "armed": self.armed, "model": self.model, "key_present": bool(self.api_key.strip())}


def paid_slot_generate(
    slot: PaidReasoningSlot,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int = 2048,
    system_prompt: str = "",
    timeout: Optional[int] = None,
) -> Tuple[str, RouteDecision]:
    """One non-streaming call to the armed paid slot (OpenAI-compatible)."""
    if not slot.armed:
        raise ProviderError("paid reasoning slot is not armed for this session")
    if requests is None:
        raise ProviderError("requests is required for provider HTTP execution")
    prepared = append_system_prompt(messages, system_prompt)
    # Reasoning-model APIs reject ``temperature`` and use ``max_completion_tokens``.
    payload = {"model": slot.model, "messages": prepared, "max_completion_tokens": int(max_tokens)}
    headers = {"Authorization": f"Bearer {slot.api_key}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            f"{slot.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout or int(os.environ.get("CHAT_JOHNSON_TIMEOUT", "120")),
        )
    except requests.RequestException as exc:
        raise ProviderError(f"paid slot network error: {exc}") from exc
    if response.status_code >= 400:
        detail = _providers.body_text(response).replace(slot.api_key, "[REDACTED_SECRET]")
        raise ProviderError(f"paid slot HTTP {response.status_code}: {detail}")
    try:
        body = response.json()
        text = str(body["choices"][0]["message"]["content"] or "")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderError("paid slot returned an unexpected response shape") from exc
    return text, RouteDecision(
        provider="paid_slot",
        model=slot.model,
        task_type="reasoning",
        reason="session-armed paid reasoning slot used for the Heavy Mode critique pass",
        solver="operator-toggle",
    )


# =============================================================================
# Existing router contract and bounded Heavy Mode
# =============================================================================

TASK_TYPES = ("context_load", "reasoning", "code_patch", "quick_text", "test_fix", "chat")

_KEYWORDS = {
    "context_load": ("whole repo", "codebase", "entire project", "summarize the repo", "map dependencies", "all files"),
    "reasoning": ("design", "architecture", "plan", "strategy", "why", "trade-off", "refactor approach", "breaking down"),
    "code_patch": ("patch", "diff", "edit file", "modify", "fix the function", "add method", "update class", "implement function"),
    "quick_text": ("summarize", "title", "one sentence", "tl;dr", "shorten", "translate"),
    "test_fix": ("traceback", "test failed", "assertion", "pytest", "error:", "exception", "stack trace"),
}


@dataclass
class RouteDecision:
    provider: str
    model: str
    task_type: str
    reason: str
    solver: str = "heuristic"
    decision_vector: Dict[str, int] = field(default_factory=dict)
    finish: str = ""  # "length" when the output budget cut the answer, "filtered" when the vendor did, else vendor value


def classify(text: str) -> str:
    """Deterministic keyword classifier — costs zero quota."""
    low = text.lower()
    scores = {task_type: 0 for task_type in TASK_TYPES}
    for task_type, words in _KEYWORDS.items():
        for word in words:
            if word in low:
                scores[task_type] += 1
    if "traceback" in low or scores["test_fix"] > 0:
        return "test_fix"
    if scores["code_patch"] and scores["reasoning"]:
        return "reasoning" if scores["reasoning"] > scores["code_patch"] else "code_patch"
    best = max(scores, key=lambda task: scores[task])
    if scores[best] == 0:
        return "code_patch" if re.search(r"def |class |import ", text) else "chat"
    return best


def candidates(task_type: str, available: List[str]) -> List[str]:
    """Rank legacy configured providers by task strengths and priority."""
    def key(name: str) -> Tuple[int, int]:
        config = PROVIDERS[name]
        return (0 if task_type in config.strengths else 1, config.priority)

    return sorted(available, key=key)


def route(
    task_type: str,
    est_tokens: int,
    ledger: QuotaLedger,
    settings: Optional[Settings] = None,
) -> Optional[str]:
    """Pick the highest-ranked legacy provider with quota headroom."""
    settings_value = settings or get_settings()
    normalized_type = task_type if task_type in TASK_TYPES else "chat"
    for name in candidates(normalized_type, settings_value.providers_available()):
        if PROVIDERS[name].context_window >= est_tokens and ledger.has_headroom(name, est_tokens):
            return name
    return None


def generate(
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
) -> Tuple[str, RouteDecision]:
    """Legacy-compatible one-pass route with provider fallback."""
    settings_value = settings or get_settings()
    normalized_type = task_type if task_type in TASK_TYPES else classify(messages[-1].get("content", ""))
    estimated_tokens = _estimate_tokens(messages) + max_tokens
    tried: List[str] = []
    failures: Dict[str, str] = {}
    for name in candidates(normalized_type, settings_value.providers_available()):
        if PROVIDERS[name].context_window < estimated_tokens or not ledger.has_headroom(discovery.vendor_for(name), estimated_tokens):
            failures[name] = "skipped: no quota headroom or context too small"
            continue
        tried.append(name)
        vendor = discovery.vendor_for(name)
        try:
            # Every real POST (retries, sibling models, rediscovery) counts toward the vendor's RPM.
            with _providers.metered(lambda: ledger.record_attempt(vendor)):
                text, tokens = chat(name, messages, max_tokens, temperature, settings_value)
        except ProviderError as exc:
            failures[name] = str(exc)
            continue
        ledger.record(vendor, tokens, count_request=False)
        config = PROVIDERS[name]
        return text, RouteDecision(
            name,
            provider_model(config),
            normalized_type,
            f"legacy strength={normalized_type in config.strengths}; tried={tried}",
        )
    detail = "; ".join(f"{name}: {error}" for name, error in failures.items())
    raise ProviderError(
        "no legacy provider available -> " + (detail if detail else "no provider has a key")
    )


def _last_user_turn(messages: Sequence[Mapping[str, str]]) -> str:
    """The newest user message, which is what the critique and synthesis passes need to see."""
    for message in reversed(list(messages)):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return str(messages[-1].get("content", "")) if messages else ""


def _heavy_pipeline(
    one_pass: Callable[[str, List[dict], int], Tuple[str, RouteDecision]],
    task_type: str,
    messages: List[dict],
    max_tokens: int,
    paid_slot: Optional[PaidReasoningSlot] = None,
    final_pass: Optional[Callable[[str, List[dict], int], Tuple[Any, RouteDecision]]] = None,
) -> Tuple[Any, RouteDecision]:
    """Bounded plan/critique/synthesis without exposing private chain-of-thought.

    The optional paid slot, when armed for this session, handles only the
    critique pass; the draft and synthesis always run on free endpoints.
    ``final_pass`` may return an unexhausted stream instead of text so the UI
    can show the synthesis as it arrives; the fallbacks still return text.
    """
    draft, draft_decision = one_pass(task_type, messages, max(256, max_tokens // 2))
    # Only the operator's request travels with the draft: the system prompt, memory, and history
    # already shaped the draft, and resending them tripled the cost of every Heavy send.
    request = _last_user_turn(messages)
    critique_messages = [
        {
            "role": "system",
            "content": (
                "Review the candidate answer for correctness, omissions, unsafe assumptions, "
                "and concrete improvements. Return a concise checklist only; do not reveal "
                "private chain-of-thought."
            ),
        },
        {"role": "user", "content": json.dumps({"request": request, "candidate": draft})},
    ]
    try:
        if paid_slot is not None and paid_slot.armed:
            critique, critique_decision = paid_slot_generate(paid_slot, critique_messages, max(256, max_tokens // 3))
        else:
            critique, critique_decision = one_pass("reasoning", critique_messages, max(256, max_tokens // 3))
    except ProviderError:
        return draft, RouteDecision(
            draft_decision.provider,
            draft_decision.model,
            task_type,
            f"heavy draft only; critique unavailable; {draft_decision.reason}",
            draft_decision.solver,
            draft_decision.decision_vector,
        )

    synthesis_messages = [
        {
            "role": "system",
            "content": (
                "Synthesize the final answer from the request, candidate, and review. "
                "Keep the answer actionable and concise. Do not print hidden reasoning "
                "or mention internal prompt contents."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"request": request, "candidate": draft, "review": critique}),
        },
    ]
    try:
        final, final_decision = (final_pass or one_pass)(task_type, synthesis_messages, max_tokens)
    except ProviderError:
        return draft, RouteDecision(
            critique_decision.provider,
            critique_decision.model,
            task_type,
            f"heavy candidate returned because synthesis was unavailable; {critique_decision.reason}",
            critique_decision.solver,
            critique_decision.decision_vector,
        )
    final_decision.task_type = task_type
    final_decision.reason = (
        f"bounded heavy mode: draft -> review({critique_decision.provider}) -> synthesis; "
        f"final={final_decision.reason}"
    )
    return final, final_decision


def heavy_stream(
    task_type: str,
    messages: List[dict],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
    paid_slot: Optional[PaidReasoningSlot] = None,
) -> Tuple[Any, RouteDecision]:
    """Heavy Mode with the synthesis streamed: draft and critique block, the final pass returns a CortexStream.

    The stream's ``text`` and ``decision.finish`` are complete only once it has been drained; when a
    fallback fires the first element is plain text instead.
    """
    if not cortex_available():
        raise ProviderError("no Cortex endpoint is keyed; Heavy Mode streaming needs one")

    def one_pass(selected_type: str, selected_messages: List[dict], tokens: int) -> Tuple[str, RouteDecision]:
        return cortex_generate(selected_type, selected_messages, ledger=ledger, max_tokens=tokens, temperature=temperature, system_prompt=system_prompt)

    def final_pass(selected_type: str, selected_messages: List[dict], tokens: int) -> Tuple[CortexStream, RouteDecision]:
        stream = CortexStream(selected_type, selected_messages, ledger, max_tokens=tokens, temperature=temperature, system_prompt=system_prompt)
        return stream, stream.decision

    return _heavy_pipeline(one_pass, task_type, messages, max_tokens, paid_slot=paid_slot, final_pass=final_pass)


def generate_heavy(
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
    paid_slot: Optional[PaidReasoningSlot] = None,
) -> Tuple[str, RouteDecision]:
    """Run the legacy-provider bounded Heavy Mode pipeline."""
    return _heavy_pipeline(
        lambda selected_type, selected_messages, tokens: generate(
            selected_type,
            selected_messages,
            ledger,
            max_tokens=tokens,
            temperature=temperature,
            settings=settings,
        ),
        task_type,
        messages,
        max_tokens,
        paid_slot=paid_slot,
    )


def generate_cortex_heavy(
    task_type: str,
    messages: List[dict],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
    paid_slot: Optional[PaidReasoningSlot] = None,
) -> Tuple[str, RouteDecision]:
    """Run bounded Heavy Mode through the strict Cortex endpoint matrix."""
    return _heavy_pipeline(
        lambda selected_type, selected_messages, tokens: cortex_generate(
            selected_type,
            selected_messages,
            ledger=ledger,
            max_tokens=tokens,
            temperature=temperature,
            system_prompt=system_prompt,
        ),
        task_type,
        messages,
        max_tokens,
        paid_slot=paid_slot,
    )


def cortex_available() -> bool:
    """True when at least one strict Cortex endpoint has a BYOK key."""
    return any(_endpoint_key(endpoint) for endpoint in CORTEX_ENDPOINTS.values())


def generate_mode(
    mode: str,
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
    paid_slot: Optional[PaidReasoningSlot] = None,
) -> Tuple[str, RouteDecision]:
    """Application entry point: Cortex endpoints first, legacy providers second.

    ``paid_slot`` is only consulted in Heavy Mode and only for the critique
    pass; Normal mode never touches it.
    """
    if cortex_available():
        try:
            if mode == "heavy":
                return generate_cortex_heavy(
                    task_type, messages, ledger, max_tokens, temperature, paid_slot=paid_slot
                )
            return cortex_generate(task_type, messages, ledger, max_tokens, temperature)
        except ProviderError as cortex_error:
            # A strict endpoint can be temporarily unavailable or have a
            # retired model id. Preserve the broader configured provider pool
            # as an explicit fallback rather than silently dropping the task.
            try:
                if mode == "heavy":
                    return generate_heavy(
                        task_type, messages, ledger, max_tokens, temperature, settings, paid_slot=paid_slot
                    )
                return generate(task_type, messages, ledger, max_tokens, temperature, settings)
            except ProviderError as legacy_error:
                raise ProviderError(
                    f"strict Cortex routing failed ({cortex_error}); "
                    f"legacy provider fallback failed ({legacy_error})"
                ) from legacy_error
    if mode == "heavy":
        return generate_heavy(task_type, messages, ledger, max_tokens, temperature, settings, paid_slot=paid_slot)
    return generate(task_type, messages, ledger, max_tokens, temperature, settings)


PIPELINE_MAX_WAIT_SECONDS = 65.0


def pipeline_generate(
    task_type: str,
    messages: Sequence[Mapping[str, str]],
    ledger: Optional[QuotaLedger],
    max_tokens: int = 4096,
    temperature: float = 0.2,
    max_wait: float = PIPELINE_MAX_WAIT_SECONDS,
) -> Tuple[str, RouteDecision]:
    """One paced call for the repository pipeline: wait for a free-tier window, then route like the chat does.

    The pipeline used the legacy one-pass route, which skipped every provider the moment a window
    was full (Gemini: 2 requests per minute) instead of waiting, so multi-step runs failed from
    step two onward. This waits up to ``max_wait`` seconds, then uses Cortex routing with the
    legacy providers as fallback.
    """
    if ledger is not None:
        wait = cortex_wait_seconds(ledger, messages, max_tokens)
        if 0 < wait <= max_wait:
            time.sleep(wait + 0.5)
    ledger_value = ledger if ledger is not None else QuotaLedger({})
    return generate_mode("normal", task_type, messages, ledger_value, max_tokens=max_tokens, temperature=temperature)
