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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

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

from .config import PROVIDERS, Settings, get_settings, provider_model
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
            value = os.environ.get(env_name, "").strip()
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


# These are deliberate routing ceilings from the Project Seth / Chat Johnson
# design. They are conservative policy limits, not guarantees from vendors.
CORTEX_ENDPOINTS: Dict[str, CortexEndpoint] = {
    "google_ai_studio": CortexEndpoint(
        name="google_ai_studio",
        label="Google AI Studio / Gemini 1.5 Pro",
        env_keys=("GEMINI_API_KEY",),
        base_url="https://generativelanguage.googleapis.com/v1beta",
        kind="gemini",
        model="gemini-1.5-pro",
        rpm_limit=2,
        tpm_limit=32_000,
        speed_score=0.48,
        context_score=1.00,
        strengths=("context_load", "reasoning", "chat"),
    ),
    "groq": CortexEndpoint(
        name="groq",
        label="Groq / Llama 3.3 70B",
        env_keys=("GROQ_API_KEY",),
        base_url="https://api.groq.com/openai/v1",
        kind="openai",
        model="llama-3.3-70b-versatile",
        rpm_limit=30,
        tpm_limit=15_000,
        speed_score=1.00,
        context_score=0.58,
        strengths=("code_patch", "quick_text", "chat"),
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


def shannon_entropy(values: Sequence[float], bins: Optional[int] = None) -> float:
    """Return histogram Shannon entropy H(x) in bits for a trajectory array."""
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
    if array.size == 0 or float(np.ptp(array)) == 0.0:
        return 0.0
    bin_count = bins or max(8, min(128, int(math.sqrt(array.size)) * 2))
    histogram, _ = np.histogram(array, bins=bin_count)
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
    power = (array_lib.abs(array_lib.fft.rfft(samples)) ** 2) / float(length)
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


def project_seth_routing_entropy(
    endpoint_names: Iterable[str],
    seed: Optional[int] = None,
    length: int = 128,
    alpha: float = 1.0,
) -> Dict[str, float]:
    """Produce normalized entropy penalties for the active routing channels."""
    names = list(endpoint_names)
    if np is None:
        return {name: 0.0 for name in names}
    maximum = max(1.0, math.log2(max(8, min(128, int(math.sqrt(length)) * 2))))
    root_seed = int(seed if seed is not None else time.time_ns() % (2**32 - 1))
    result: Dict[str, float] = {}
    for index, name in enumerate(names):
        noise = generate_one_over_f_noise(
            length=length,
            alpha=alpha,
            seed=root_seed + index,
            sample_rate=100.0,
            low_frequency_hz=max(0.01, 100.0 / length),
        )
        # The routing penalty is computed from the integrated trajectory, not
        # from a raw noise series.  Coefficients vary only to create independent
        # deterministic channels; this is an experimental signal, not a quality
        # measurement or a physical observable.
        trajectory = simulate_project_seth_trajectory(
            initial_x=0.0,
            eta=noise.samples,
            dt=0.01,
            sigma=0.12,
            A=0.55 + 0.08 * index,
            C=0.01 * (index + 1),
        )
        trajectory_entropy = shannon_entropy(trajectory)
        result[name] = max(0.0, min(1.0, trajectory_entropy / maximum))
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


def _ensure_cortex_ledger(ledger: Optional[QuotaLedger]) -> None:
    if ledger is None:
        return
    for endpoint in CORTEX_ENDPOINTS.values():
        try:
            ledger.usage(endpoint.name)
        except KeyError:
            ledger.register(
                endpoint.name,
                endpoint.rpm_limit,
                endpoint.tpm_limit if endpoint.tpm_limit is not None else 10**9,
            )


def _endpoint_key(endpoint: CortexEndpoint) -> str:
    for env_name in endpoint.env_keys:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def _endpoint_usage(
    endpoint: CortexEndpoint,
    ledger: Optional[QuotaLedger],
    current_usage: Optional[Mapping[str, Mapping[str, float]]],
) -> Tuple[float, float]:
    if current_usage and endpoint.name in current_usage:
        row = current_usage[endpoint.name]
        return float(row.get("rpm_used", 0.0)), float(row.get("tpm_used", 0.0))
    if ledger is not None:
        try:
            row = ledger.usage(endpoint.name)
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


def _feasible_endpoint(
    endpoint: CortexEndpoint,
    rpm_used: float,
    tpm_used: float,
    estimated_tokens: int,
) -> bool:
    if rpm_used + 1.0 > endpoint.rpm_limit:
        return False
    if endpoint.tpm_limit is not None and tpm_used + estimated_tokens > endpoint.tpm_limit:
        return False
    return bool(_endpoint_key(endpoint))


def _build_constraint_array(
    endpoints: Sequence[CortexEndpoint],
    usage: Mapping[str, Tuple[float, float]],
    estimated_tokens: int,
    enabled: Mapping[str, bool],
) -> Any:
    """Build the unbending equality/capacity constraints for scipy.milp."""
    if np is None or LinearConstraint is None:
        return None
    array_lib = _require_numpy()

    rows: List[Any] = [array_lib.ones(len(endpoints), dtype=float)]
    lower: List[float] = [1.0]
    upper: List[float] = [1.0]

    for index, endpoint in enumerate(endpoints):
        row = array_lib.zeros(len(endpoints), dtype=float)
        row[index] = 1.0
        rpm_used, _ = usage[endpoint.name]
        rows.append(row)
        lower.append(-array_lib.inf)
        upper.append(float(max(0.0, endpoint.rpm_limit - rpm_used)))

    for index, endpoint in enumerate(endpoints):
        if endpoint.tpm_limit is None:
            continue
        row = array_lib.zeros(len(endpoints), dtype=float)
        row[index] = float(estimated_tokens)
        _, tpm_used = usage[endpoint.name]
        rows.append(row)
        lower.append(-array_lib.inf)
        upper.append(float(max(0.0, endpoint.tpm_limit - tpm_used)))

    # A missing key or explicit exclusion is represented as x_i <= 0. This is
    # part of the linear constraint system rather than a post-solver mutation.
    for index, endpoint in enumerate(endpoints):
        if not enabled.get(endpoint.name, False):
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = 1.0
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(0.0)

    if LinearConstraint is None:
        return None
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
    first row enforces sum(x)=1, followed by per-endpoint RPM and TPM caps.
    When SciPy is not installed, the same feasible set is evaluated with a
    deterministic binary fallback; no request is sent outside the caps.
    """
    if estimated_tokens < 1:
        raise ValueError("estimated_tokens must be at least one")
    _ensure_cortex_ledger(ledger)
    excluded_set = set(excluded or ())
    endpoints = list(CORTEX_ENDPOINTS.values())
    usage = {
        endpoint.name: _endpoint_usage(endpoint, ledger, current_usage)
        for endpoint in endpoints
    }
    supplied_entropy = dict(entropy_by_endpoint or {})
    if not supplied_entropy:
        supplied_entropy = project_seth_routing_entropy(
            (endpoint.name for endpoint in endpoints),
            length=128,
        )
    penalties = {
        endpoint.name: float(supplied_entropy.get(endpoint.name, 0.0))
        for endpoint in endpoints
    }
    utilities = {
        endpoint.name: _utility_score(endpoint, task_type, penalties[endpoint.name])
        for endpoint in endpoints
    }
    enabled = {
        endpoint.name: (
            endpoint.name not in excluded_set
            and bool(_endpoint_key(endpoint))
            and _feasible_endpoint(endpoint, usage[endpoint.name][0], usage[endpoint.name][1], estimated_tokens)
        )
        for endpoint in endpoints
    }
    feasible = [endpoint for endpoint in endpoints if enabled[endpoint.name]]
    if not feasible:
        raise ProviderError(
            "Cortex 2 found no BYOK endpoint with RPM/TPM headroom; "
            "add a permitted key or wait for the provider window to roll over."
        )

    constraint = _build_constraint_array(endpoints, usage, estimated_tokens, enabled)
    decision_vector: Dict[str, int]
    solver_name = "deterministic-binary-fallback"
    chosen: Optional[CortexEndpoint] = None

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

    if chosen is None:
        chosen = max(feasible, key=lambda endpoint: (utilities[endpoint.name], -endpoints.index(endpoint)))

    decision_vector = {
        endpoint.name: int(endpoint.name == chosen.name) for endpoint in endpoints
    }
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


def build_cortex_request(
    endpoint: str | CortexEndpoint,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
    temperature: float,
    stream: bool,
    system_prompt: str = "",
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Format a provider-specific URL, redacted-safe headers, and JSON body."""
    selected = _as_endpoint(endpoint)
    prepared = append_system_prompt(messages, system_prompt)
    key = _endpoint_key(selected)
    if not key:
        raise ProviderError(f"no BYOK key configured for {selected.name}")

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
        url = f"{selected.base_url}/models/{selected.model}:streamGenerateContent?alt=sse" if stream else f"{selected.base_url}/models/{selected.model}:generateContent"
        return url, {"x-goog-api-key": key, "Content-Type": "application/json"}, payload

    payload = {
        "model": selected.model,
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
    for raw_line in response.iter_lines(decode_unicode=True):
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
            yield parsed


def cortex_stream(
    endpoint: str | CortexEndpoint,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
    timeout: Optional[int] = None,
) -> Iterator[str]:
    """Yield generated text chunks from a MILP-selected provider endpoint."""
    if requests is None:
        raise ProviderError("requests is required for provider HTTP execution")
    selected = _as_endpoint(endpoint)
    url, headers, payload = build_cortex_request(
        selected, messages, max_tokens, temperature, stream=True, system_prompt=system_prompt
    )
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout or int(os.environ.get("CHAT_JOHNSON_TIMEOUT", "120")),
            stream=True,
        )
    except requests.RequestException as exc:
        raise ProviderError(f"{selected.name} network error: {exc}") from exc

    try:
        if response.status_code >= 400:
            detail = response.text[:400]
            secret = _endpoint_key(selected)
            if secret:
                detail = detail.replace(secret, "[REDACTED_SECRET]")
            raise ProviderError(f"{selected.name} HTTP {response.status_code}: {detail}")
        emitted = False
        for payload_item in _iter_sse_payloads(response):
            chunk = _extract_stream_text(selected, payload_item)
            if chunk:
                emitted = True
                yield chunk
        if not emitted:
            # Some compatible gateways return one JSON object even when stream
            # mode is requested. The response has already been consumed safely.
            return
    finally:
        response.close()


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
    """Run Cortex 2 selection, then consume the Cortex 1 generation stream."""
    estimated_tokens = _estimate_tokens(messages) + max_tokens
    excluded: set[str] = set()
    attempted: List[str] = []
    last_error: Optional[Exception] = None
    for _ in range(len(CORTEX_ENDPOINTS)):
        decision = select_milp_endpoint(
            task_type,
            estimated_tokens,
            ledger=ledger,
            excluded=excluded,
        )
        endpoint = decision.endpoint
        attempted.append(endpoint.name)
        try:
            chunks = cortex_stream(
                endpoint,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                system_prompt=system_prompt,
            )
            text = "".join(chunks)
            tokens = _estimate_tokens(messages, text)
            if ledger is not None:
                _ensure_cortex_ledger(ledger)
                ledger.record(endpoint.name, tokens)
            route_decision = RouteDecision(
                provider=endpoint.name,
                model=endpoint.model,
                task_type=task_type,
                reason=f"{decision.reason}; solver={decision.solver}; attempted={attempted}",
                solver=decision.solver,
                decision_vector=decision.decision_vector,
            )
            return text, route_decision
        except ProviderError as exc:
            excluded.add(endpoint.name)
            last_error = exc

    raise ProviderError(
        f"all Cortex endpoints failed; attempted={attempted}; last_error={last_error}"
    )


def stream_generation(
    task_type: str,
    messages: Sequence[Mapping[str, str]],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
) -> Iterator[str]:
    """Select one endpoint with MILP and expose its live text stream."""
    estimated_tokens = _estimate_tokens(messages) + max_tokens
    decision = select_milp_endpoint(task_type, estimated_tokens, ledger=ledger)
    emitted: List[str] = []
    for chunk in cortex_stream(
        decision.endpoint,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=system_prompt,
    ):
        emitted.append(chunk)
        yield chunk
    if ledger is not None:
        _ensure_cortex_ledger(ledger)
        ledger.record(decision.endpoint.name, _estimate_tokens(messages, "".join(emitted)))


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
    for name in candidates(normalized_type, settings_value.providers_available()):
        if PROVIDERS[name].context_window < estimated_tokens or not ledger.has_headroom(name, estimated_tokens):
            continue
        tried.append(name)
        try:
            text, tokens = chat(name, messages, max_tokens, temperature, settings_value)
        except ProviderError:
            continue
        ledger.record(name, tokens)
        config = PROVIDERS[name]
        return text, RouteDecision(
            name,
            provider_model(config),
            normalized_type,
            f"legacy strength={normalized_type in config.strengths}; tried={tried}",
        )
    raise ProviderError(
        "no legacy provider available: "
        + (f"tried {tried}" if tried else "check API keys / quota ledger")
    )


def _heavy_pipeline(
    one_pass: Callable[[str, List[dict], int], Tuple[str, RouteDecision]],
    task_type: str,
    messages: List[dict],
    max_tokens: int,
) -> Tuple[str, RouteDecision]:
    """Bounded plan/critique/synthesis without exposing private chain-of-thought."""
    draft, draft_decision = one_pass(task_type, messages, max(256, max_tokens // 2))
    critique_messages = [
        {
            "role": "system",
            "content": (
                "Review the candidate answer for correctness, omissions, unsafe assumptions, "
                "and concrete improvements. Return a concise checklist only; do not reveal "
                "private chain-of-thought."
            ),
        },
        {"role": "user", "content": json.dumps({"request": messages, "candidate": draft})},
    ]
    try:
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
            "content": json.dumps({"request": messages, "candidate": draft, "review": critique}),
        },
    ]
    try:
        final, final_decision = one_pass(task_type, synthesis_messages, max_tokens)
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
    final_decision.reason = f"bounded heavy mode: draft -> review -> synthesis; final={final_decision.reason}"
    return final, final_decision


def generate_heavy(
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
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
    )


def generate_cortex_heavy(
    task_type: str,
    messages: List[dict],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
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
    )


def generate_mode(
    mode: str,
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
) -> Tuple[str, RouteDecision]:
    """Application entry point: Cortex endpoints first, legacy providers second."""
    cortex_names = [name for name in CORTEX_ENDPOINTS if _endpoint_key(CORTEX_ENDPOINTS[name])]
    if cortex_names:
        try:
            if mode == "heavy":
                return generate_cortex_heavy(task_type, messages, ledger, max_tokens, temperature)
            return cortex_generate(task_type, messages, ledger, max_tokens, temperature)
        except ProviderError as cortex_error:
            # A strict endpoint can be temporarily unavailable or have a
            # retired model id. Preserve the broader configured provider pool
            # as an explicit fallback rather than silently dropping the task.
            try:
                if mode == "heavy":
                    return generate_heavy(task_type, messages, ledger, max_tokens, temperature, settings)
                return generate(task_type, messages, ledger, max_tokens, temperature, settings)
            except ProviderError as legacy_error:
                raise ProviderError(
                    f"strict Cortex routing failed ({cortex_error}); "
                    f"legacy provider fallback failed ({legacy_error})"
                ) from legacy_error
    if mode == "heavy":
        return generate_heavy(task_type, messages, ledger, max_tokens, temperature, settings)
    return generate(task_type, messages, ledger, max_tokens, temperature, settings)
