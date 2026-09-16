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
import threading
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
from . import pinkwave
from .quota import QuotaLedger, seconds_to_utc_midnight
from .quota_registry import credential_fingerprint, get_quota_ledger

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
    """Which BYOK providers have a key in this context (session overlay first, then the environment).

    Never holds key values: the HTTP adapter resolves a key at request time through
    :func:`_endpoint_key`, so nothing plaintext lives in a module dictionary.
    """
    vault: Dict[str, Dict[str, Any]] = {}
    for provider, env_names in BYOK_ENV_KEYS.items():
        selected_name = next((env_name for env_name in env_names if resolve_secret(env_name)), "")
        vault[provider] = {"provider": provider, "env_key": selected_name or env_names[0], "configured": bool(selected_name)}
    return vault


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
    max_output_tokens: int = 8_192  # the largest single answer the model can write; a page request needs a big one
    rpd_limit: int = 0  # requests per UTC day the vendor allows (0 = no such cap); CHAT_JOHNSON_RPD_<VENDOR> overrides


# Shared with the legacy provider client: one live-id cache per vendor.
_DISCOVERED_MODELS = discovery.DISCOVERED


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
        rpm_limit=5,  # half of the published Flash free-tier 10 RPM; a 429 still backs off. CHAT_JOHNSON_RPM_GEMINI overrides.
        tpm_limit=32_000,
        speed_score=0.48,
        context_score=1.00,
        strengths=("context_load", "reasoning", "chat"),
        model_env="CORTEX_GEMINI_MODEL",
        max_output_tokens=65_536,
        rpd_limit=200,  # the Flash free tier also counts requests a day (250 published); a margin for retries and probes
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
        max_output_tokens=32_768,
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

# A page or app is the one answer that must not be cut: it gets a bigger budget and only endpoints that can write it.
PAGE_OUTPUT_TOKENS = 6_144        # below this a page request is not worth the output-need rows
INTERFACE_OUTPUT_CAP = 16_384     # the automatic budget raise for a page request stops here
OUTPUT_RESERVE_TOKENS = 500       # prompt slack kept out of the output ceiling
CONTINUATION_ROUNDS = 3
CONTINUATION_TAIL_CHARS = 240
CONTINUATION_MIN_OVERLAP = 12
_INTERFACE_RE = re.compile(
    r"\b(?:apps?|application|web ?pages?|pages?|website|site|landing|dashboard|interface|ui|mock-?ups?|prototype|games?|"
    r"widget|calculator|quiz|flashcards?|study guide|to-?do|html|css|buttons?|preview|canvas)\b",
    re.I,
)


def is_interface_request(text: str) -> bool:
    """Whether the request asks for a page, app or interface (or refers to the one on the canvas)."""
    head = (text or "")[:2_000]
    return "```html" in head or bool(_INTERFACE_RE.search(head))


def keyed_endpoints() -> List[CortexEndpoint]:
    return [endpoint for endpoint in CORTEX_ENDPOINTS.values() if _endpoint_key(endpoint)]


def output_ceiling(endpoint: CortexEndpoint, prompt_tokens: int) -> int:
    """The largest answer this endpoint can write after the prompt: its model ceiling, bounded by its minute window."""
    tpm = effective_tpm(endpoint)
    by_window = (int(tpm) - int(prompt_tokens) - OUTPUT_RESERVE_TOKENS) if tpm else endpoint.max_output_tokens
    return max(0, min(int(endpoint.max_output_tokens), by_window))


def effective_output_budget(messages: Sequence[Mapping[str, str]], sidebar_budget: int, interface: bool) -> int:
    """The output budget a send really gets: the slider, raised for a page request up to what a keyed endpoint can write."""
    budget = int(sidebar_budget)
    if not interface:
        return budget
    prompt_tokens = _estimate_tokens(messages)
    ceilings = [output_ceiling(endpoint, prompt_tokens) for endpoint in keyed_endpoints()]
    best = max(ceilings) if ceilings else budget
    return max(budget, min(INTERFACE_OUTPUT_CAP, best))


def open_fence(text: str) -> bool:
    return (text or "").count("```") % 2 == 1


def needs_continuation(text: str, finish: str) -> bool:
    """A cut inside a fenced block is the one truncation the app finishes on its own."""
    return finish == "length" and open_fence(text)


def continuation_messages(messages: Sequence[Mapping[str, str]], answer: str, tail_chars: int = CONTINUATION_TAIL_CHARS) -> List[dict]:
    """The original conversation plus the cut answer, then the order to continue it verbatim from its tail."""
    tail = answer[-tail_chars:]
    order = (
        f"CONTINUATION: your previous answer was cut by the output budget after {len(answer) // 4} tokens. Continue it "
        "EXACTLY from the end of this tail, character for character: no preamble, no repeated lines, no new fence "
        "opener, only the missing rest, then close the fence.\n[TAIL]\n" + tail + "\n[/TAIL]"
    )
    return [dict(m) for m in messages] + [{"role": "assistant", "content": answer}, {"role": "user", "content": order}]


_FENCE_OPENER = re.compile(r"^\s*```[a-zA-Z0-9_-]*[ \t]*\n?")


def stitch(answer: str, continuation: str, max_overlap: int = CONTINUATION_TAIL_CHARS) -> str:
    """Join a continuation onto the cut answer, dropping a repeated fence opener and the longest overlapping tail."""
    piece = _FENCE_OPENER.sub("", continuation, count=1) if open_fence(answer) else continuation
    longest = min(len(answer), len(piece), max_overlap)
    for size in range(longest, CONTINUATION_MIN_OVERLAP - 1, -1):
        if answer.endswith(piece[:size]):
            return answer + piece[size:]
    return answer + piece


def continue_answer(
    task_type: str,
    messages: Sequence[Mapping[str, str]],
    answer: str,
    decision: "RouteDecision",
    ledger: Optional[QuotaLedger],
    max_tokens: int,
    rounds: int = CONTINUATION_ROUNDS,
    on_round: Optional[Callable[[int], None]] = None,
) -> Tuple[str, "RouteDecision", int]:
    """Finish an answer that was cut inside a fence: up to ``rounds`` verbatim continuations, stitched into one text.

    Runs single passes in normal mode (a Heavy pass would redraft the page). Returns the stitched answer, the last
    decision (its ``finish`` says whether the text is now complete) and the number of continuations used.
    """
    used = 0
    text, last = answer, decision
    while used < rounds and needs_continuation(text, last.finish):
        used += 1
        if on_round is not None:
            on_round(used)
        piece, last = cortex_generate(
            task_type, continuation_messages(messages, text), ledger=ledger, max_tokens=max_tokens, temperature=0.1,
            output_need=max_tokens,
        )
        piece = strip_reasoning_tags(piece)
        if not piece.strip():
            break
        text = stitch(text, piece)
        last.reason = f"{decision.reason}; continued {used}x"
    return text, last, used


_FRAGMENT_START = re.compile(r"^\s*[\w-]+\s*[=\"']|^\s*[\w-]+\"\s|^\s*[)\]};,]")
_DELIBERATION_START = re.compile(r"^\s*(?:```\s*)?(?:Wait|Let's|Let me|Okay,|Hmm|First,|I need to|We need to|Let us)\b", re.I)


def answer_shape_problem(text: str) -> str:
    """Why a stored answer would be garbage: '' when it looks like an answer, else the problem in a few words.

    gpt-oss splits output across a reasoning channel the app drops and a content channel it keeps, so a page can
    arrive as a fragment starting mid-attribute, or as deliberation that never reaches the page.
    """
    body = (text or "").strip()
    if not body:
        return "empty"
    if _FRAGMENT_START.match(body) and "```" not in body[:200]:
        return "starts mid-tag or mid-attribute (a fragment, not an answer)"
    if _DELIBERATION_START.match(body) and "```html" not in body and len(body) < 1_500:
        return "deliberation instead of an answer"
    return ""


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
_TELEMETRY_LOCK = threading.Lock()


def _telemetry_key(endpoint_name: str) -> str:
    # Per credential: one visitor's rejected key must not lower another visitor's endpoint score.
    return f"{endpoint_name}|{credential_fingerprint()}"


def record_telemetry(endpoint_name: str, latency_seconds: float, ok: bool) -> None:
    with _TELEMETRY_LOCK:
        bucket = ENDPOINT_TELEMETRY.setdefault(_telemetry_key(endpoint_name), deque(maxlen=TELEMETRY_WINDOW))
        bucket.append((max(0.0, float(latency_seconds)), bool(ok)))


def telemetry_snapshot(endpoint_name: str) -> Dict[str, float]:
    """Observed failure rate and latency ratio for an endpoint (zeros when unobserved)."""
    with _TELEMETRY_LOCK:
        bucket = list(ENDPOINT_TELEMETRY.get(_telemetry_key(endpoint_name)) or ())
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
    runner_up: str = ""                      # the feasible endpoint that would have served without the winner
    chaos: Dict[str, Any] = field(default_factory=dict)  # gain, profile, step, jitter of the winner (empty when no wave is active)
    explored: bool = False                   # the wave sent this request to a runner-up to keep the estimates honest
    learned: Dict[str, Any] = field(default_factory=dict)  # the learner's inputs for the chosen endpoint (empty when off)


def _vendor(endpoint: "CortexEndpoint") -> str:
    """The ledger bucket for an endpoint: one bucket per credential/vendor."""
    return discovery.vendor_for(endpoint.name)


def effective_rpm(endpoint: "CortexEndpoint") -> int:
    """The endpoint's requests-per-minute ceiling: ``CHAT_JOHNSON_RPM_<VENDOR>`` (session or environment) over the table."""
    override = resolve_secret(f"CHAT_JOHNSON_RPM_{_vendor(endpoint).upper()}")
    return int(override) if override.isdigit() and int(override) > 0 else int(endpoint.rpm_limit)


def effective_tpm(endpoint: "CortexEndpoint") -> Optional[int]:
    """The endpoint's tokens-per-minute ceiling: ``CHAT_JOHNSON_TPM_<VENDOR>`` over the table; 0 means uncapped (None)."""
    override = resolve_secret(f"CHAT_JOHNSON_TPM_{_vendor(endpoint).upper()}")
    if override.isdigit():
        return int(override) or None
    return endpoint.tpm_limit


def effective_rpd(endpoint: "CortexEndpoint") -> int:
    """The endpoint's requests-per-day ceiling: ``CHAT_JOHNSON_RPD_<VENDOR>`` over the table; 0 means no such cap."""
    override = resolve_secret(f"CHAT_JOHNSON_RPD_{_vendor(endpoint).upper()}")
    if override.isdigit():
        return int(override)
    return int(endpoint.rpd_limit)


def _ensure_cortex_ledger(ledger: Optional[QuotaLedger]) -> None:
    """Register each Cortex endpoint's vendor bucket, tightening to the stricter policy.

    The legacy registry may already have registered the same vendor (for example
    ``gemini`` at 15 RPM); one key must be metered once, so the tighter of the
    two ceilings applies to both routing paths.
    """
    if ledger is None:
        return
    for endpoint in CORTEX_ENDPOINTS.values():
        tpm = effective_tpm(endpoint)
        ledger.tighten(
            _vendor(endpoint),
            effective_rpm(endpoint),
            tpm if tpm is not None else 10**9,
        )
        rpd = effective_rpd(endpoint)
        if rpd > 0:
            ledger.tighten_daily_requests(_vendor(endpoint), rpd)


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
    """Characters of project memory a chat request may carry alongside the output budget.

    4 chars per token as in _estimate_tokens, a 500-token reserve for the system prompt and the
    request itself, never below 8k and never above the 24k default. The budget follows the WIDEST
    keyed endpoint, not the narrowest: the solver's TPM constraint already steers a long request
    away from a narrow endpoint (Groq's 8,000 TPM), so sizing memory to that endpoint only starved
    every chat of its own history whenever Groq was keyed and the output budget was raised.
    """
    widest: Optional[int] = None
    uncapped = False
    for endpoint in CORTEX_ENDPOINTS.values():
        if not _endpoint_key(endpoint):
            continue
        tpm = effective_tpm(endpoint)
        if tpm is None:
            uncapped = True  # an uncapped keyed endpoint takes the whole default window
            continue
        room = 4 * (int(tpm) - int(max_tokens) - CONTEXT_RESERVE_TOKENS)
        widest = room if widest is None else max(widest, room)
    cap = DEFAULT_CONTEXT_CHARS if uncapped or widest is None else widest
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
    """Seconds until some keyed Cortex endpoint has RPM/TPM headroom for this request; 0 when one has it now.

    An endpoint that can never take the request (its whole per-minute ceiling is smaller than the
    request, or its daily cap is reached) is left out: its "no wait" used to hide the real wait of
    the endpoint that could take it, so the chat sent at once and failed instead of pausing.
    """
    if ledger is None:
        return 0.0
    _ensure_cortex_ledger(ledger)
    estimated = _estimate_tokens(messages) + int(max_tokens)
    waits = []
    for endpoint in CORTEX_ENDPOINTS.values():
        if not _endpoint_key(endpoint):
            continue
        usage = _endpoint_usage(endpoint, ledger, None)
        ceiling = usage.tpm_ceiling(endpoint)
        if ceiling is not None and estimated > ceiling:
            continue
        if usage.daily_limit > 0 and usage.daily_used + estimated > usage.daily_limit:
            continue
        if usage.daily_request_limit > 0 and usage.daily_requests >= usage.daily_request_limit:
            continue
        waits.append(ledger.wait_seconds(_vendor(endpoint), estimated))
    return float(min(waits)) if waits else 0.0


HEADROOM_MARKERS = ("headroom", "requests used in the last minute", "would exceed", "asked to wait")
CHAT_MAX_WAIT_SECONDS = 65.0  # one free-tier window; longer waits surface as the plain error instead


def headroom_wait_seconds(exc: BaseException, ledger: Optional[QuotaLedger], messages: Sequence[Mapping[str, str]], max_tokens: int, max_wait: float = CHAT_MAX_WAIT_SECONDS) -> float:
    """Seconds to pause before retrying a failure that was only a full free-tier window; 0 for any other failure or a wait past ``max_wait``."""
    text = str(exc).lower()
    if not any(marker in text for marker in HEADROOM_MARKERS):
        return 0.0
    wait = cortex_wait_seconds(ledger, messages, max_tokens)
    return float(wait) if 0.0 < wait <= max_wait else 0.0


def _pacer(ledger: Optional[QuotaLedger]) -> Optional[Callable[[List[dict], int], None]]:
    """Before each Heavy pass: wait for a free-tier window (up to one) instead of failing the pass.

    Gemini allows a handful of requests per minute, so the second and third passes of one Heavy
    send often landed inside the window opened by the first and were silently replaced by the draft.
    """
    if ledger is None:
        return None

    def pace(selected_messages: List[dict], tokens: int) -> None:
        wait = cortex_wait_seconds(ledger, selected_messages, tokens)
        if 0.0 < wait <= CHAT_MAX_WAIT_SECONDS:
            time.sleep(wait + 0.5)

    return pace


@dataclass(frozen=True)
class EndpointUsage:
    """What the capacity rows see for one endpoint: window use plus the effective ceilings and the daily cap."""

    rpm_used: float = 0.0
    tpm_used: float = 0.0
    daily_used: float = 0.0
    daily_limit: float = 0.0   # 0 = uncapped
    rpm_limit: float = 0.0     # 0 = use the endpoint table
    tpm_limit: float = 0.0
    daily_requests: float = 0.0
    daily_request_limit: float = 0.0  # 0 = no request cap per day
    blocked_for: float = 0.0   # seconds the vendor asked the app to wait (a 429 with a reset hint)

    def rpm_ceiling(self, endpoint: CortexEndpoint) -> float:
        table = float(effective_rpm(endpoint))
        return min(table, self.rpm_limit) if self.rpm_limit > 0 else table

    def tpm_ceiling(self, endpoint: CortexEndpoint) -> Optional[float]:
        tpm = effective_tpm(endpoint)
        table = float(tpm) if tpm is not None else None
        if self.tpm_limit > 0 and self.tpm_limit < 10**9:
            return min(table, self.tpm_limit) if table is not None else self.tpm_limit
        return table


def _usage_from_row(row: Mapping[str, float]) -> EndpointUsage:
    return EndpointUsage(
        rpm_used=float(row.get("rpm_used", 0.0)), tpm_used=float(row.get("tpm_used", 0.0)),
        daily_used=float(row.get("daily_tokens", 0.0)), daily_limit=float(row.get("daily_limit", 0.0)),
        rpm_limit=float(row.get("rpm_limit", 0.0)), tpm_limit=float(row.get("tpm_limit", 0.0)),
        daily_requests=float(row.get("daily_requests", 0.0)), daily_request_limit=float(row.get("daily_request_limit", 0.0)),
        blocked_for=float(row.get("blocked_for", 0.0)),
    )


def _endpoint_usage(
    endpoint: CortexEndpoint,
    ledger: Optional[QuotaLedger],
    current_usage: Optional[Mapping[str, Mapping[str, float]]],
) -> EndpointUsage:
    for key in (endpoint.name, _vendor(endpoint)):
        if current_usage and key in current_usage:
            return _usage_from_row(current_usage[key])
    if ledger is not None:
        try:
            return _usage_from_row(ledger.usage(_vendor(endpoint)))
        except KeyError:
            return EndpointUsage()
    return EndpointUsage()


QUALITY_WEIGHT = 0.30  # learned quality shifts utility by QUALITY_WEIGHT * (posterior mean - 0.5): at most +-0.15


def _utility_score(
    endpoint: CortexEndpoint, task_type: str, entropy_penalty: float,
    speed: Optional[float] = None, quality: Optional[float] = None, modulation: float = 0.0,
) -> float:
    """Task fit from speed and context ability, plus the task bonus, minus the bounded entropy penalty.

    ``speed`` is the learner's measured value when one exists (the table value otherwise); ``quality``
    is the learned posterior mean (0.5 is neutral); ``modulation`` is the dynamics layer's bounded term.
    """
    context_weight = {
        "context_load": 0.92,
        "reasoning": 0.72,
        "test_fix": 0.56,
        "code_patch": 0.30,
        "quick_text": 0.12,
        "chat": 0.35,
    }.get(task_type, 0.35)
    speed_value = float(endpoint.speed_score if speed is None else speed)
    raw = (1.0 - context_weight) * speed_value + context_weight * endpoint.context_score
    task_bonus = 0.10 if task_type in endpoint.strengths else 0.0
    quality_term = QUALITY_WEIGHT * (max(0.0, min(1.0, float(quality))) - 0.5) if quality is not None else 0.0
    # Higher channel entropy is a bounded penalty, not a scientific claim that
    # the random realization measures provider quality.
    return float(raw + task_bonus + quality_term + float(modulation) - 0.30 * max(0.0, min(1.0, entropy_penalty)))


def _capacity_reasons(endpoint: CortexEndpoint, usage: EndpointUsage, estimated_tokens: int) -> List[str]:
    """Human-readable reasons an endpoint cannot take this request right now (the same rows the solver enforces)."""
    reasons: List[str] = []
    rpm_limit = usage.rpm_ceiling(endpoint)
    if usage.rpm_used + 1.0 > rpm_limit:
        reasons.append(f"{int(usage.rpm_used)}/{int(rpm_limit)} requests used in the last minute")
    tpm_limit = usage.tpm_ceiling(endpoint)
    if tpm_limit is not None:
        if estimated_tokens > tpm_limit:
            reasons.append(
                f"request needs ~{estimated_tokens} tokens but the ceiling is {int(tpm_limit)} TPM "
                "(lower the output token budget or shorten the prompt)"
            )
        elif usage.tpm_used + estimated_tokens > tpm_limit:
            reasons.append(f"~{int(usage.tpm_used)}+{estimated_tokens} tokens would exceed {int(tpm_limit)} TPM this minute")
    if usage.daily_limit > 0 and usage.daily_used + estimated_tokens > usage.daily_limit:
        hours = max(1, int(seconds_to_utc_midnight() // 3600) + 1)
        reasons.append(f"daily cap of {int(usage.daily_limit)} tokens reached ({int(usage.daily_used)} used); resets in about {hours} h")
    if usage.daily_request_limit > 0 and usage.daily_requests >= usage.daily_request_limit:
        hours = max(1, int(seconds_to_utc_midnight() // 3600) + 1)
        reasons.append(f"{int(usage.daily_requests)}/{int(usage.daily_request_limit)} requests used today; resets in about {hours} h")
    if usage.blocked_for > 0:
        reasons.append(f"the vendor asked to wait {int(usage.blocked_for) + 1} s (HTTP 429)")
    return reasons


def _build_constraint_array(
    endpoints: Sequence[CortexEndpoint],
    usage: Mapping[str, EndpointUsage],
    estimated_tokens: int,
    enabled: Mapping[str, bool],
) -> Any:
    """Constraint rows for scipy.milp: exclusivity, RPM, TPM, daily cap, key/exclusion.

    These rows decide feasibility. ``enabled`` only encodes "has a key and is not
    excluded"; the RPM/TPM/daily ceilings are enforced here, by the solver, against the
    tighter of the endpoint table and the ledger's own limits.
    """
    if np is None or LinearConstraint is None:
        return None
    array_lib = _require_numpy()

    rows: List[Any] = [array_lib.ones(len(endpoints), dtype=float)]
    lower: List[float] = [1.0]
    upper: List[float] = [1.0]

    for index, endpoint in enumerate(endpoints):
        use = usage[endpoint.name]
        row = array_lib.zeros(len(endpoints), dtype=float)
        row[index] = 1.0
        rows.append(row)
        lower.append(-array_lib.inf)
        upper.append(float(max(0.0, use.rpm_ceiling(endpoint) - use.rpm_used)))  # x_i <= remaining requests
        tpm_limit = use.tpm_ceiling(endpoint)
        if tpm_limit is not None:
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = float(estimated_tokens)
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(float(max(0.0, tpm_limit - use.tpm_used)))  # tokens * x_i <= remaining tokens
        if use.daily_limit > 0:
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = float(estimated_tokens)
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(float(max(0.0, use.daily_limit - use.daily_used)))  # tokens * x_i <= remaining today
        if use.daily_request_limit > 0:
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = 1.0
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(float(max(0.0, use.daily_request_limit - use.daily_requests)))  # x_i <= remaining requests today
        if use.blocked_for > 0:
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = 1.0
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(0.0)  # the vendor asked to wait: x_i <= 0
        if not enabled.get(endpoint.name, False):
            row = array_lib.zeros(len(endpoints), dtype=float)
            row[index] = 1.0
            rows.append(row)
            lower.append(-array_lib.inf)
            upper.append(0.0)  # no key / excluded: x_i <= 0
    return LinearConstraint(array_lib.asarray(rows), array_lib.asarray(lower), array_lib.asarray(upper))


def feasibility_rows(
    estimated_tokens: int,
    ledger: Optional[QuotaLedger] = None,
    current_usage: Optional[Mapping[str, Mapping[str, float]]] = None,
    excluded: Optional[Iterable[str]] = None,
) -> Tuple[List[CortexEndpoint], Dict[str, EndpointUsage], Dict[str, bool], Dict[str, List[str]]]:
    """The capacity rows every selection sees: endpoints, their usage, which are enabled (key, not excluded), and why the rest are blocked.

    Shared by the selector and the Monte Carlo proctor so a simulation can never find an endpoint feasible that production would not.
    """
    excluded_set = set(excluded or ())
    endpoints = list(CORTEX_ENDPOINTS.values())
    usage = {endpoint.name: _endpoint_usage(endpoint, ledger, current_usage) for endpoint in endpoints}
    enabled = {endpoint.name: endpoint.name not in excluded_set and bool(_endpoint_key(endpoint)) for endpoint in endpoints}
    blocked: Dict[str, List[str]] = {}
    for endpoint in endpoints:
        if not enabled[endpoint.name]:
            blocked[endpoint.name] = ["no key configured" if not _endpoint_key(endpoint) else "already tried this request"]
            continue
        reasons = _capacity_reasons(endpoint, usage[endpoint.name], estimated_tokens)
        if reasons:
            blocked[endpoint.name] = reasons
    return endpoints, usage, enabled, blocked


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
    endpoints, usage, enabled, blocked = feasibility_rows(estimated_tokens, ledger, current_usage, excluded_set)
    supplied_entropy = dict(entropy_by_endpoint or {})
    if not supplied_entropy:
        supplied_entropy = project_seth_routing_entropy((endpoint.name for endpoint in endpoints), length=128)
    penalties = {endpoint.name: float(supplied_entropy.get(endpoint.name, 0.0)) for endpoint in endpoints}
    # Controlled chaos: the scope's pink wave adds a bounded jitter so near-ties break differently over time;
    # the capacity rows below are untouched, so it can never select a blocked endpoint.
    chaos = pinkwave.current()
    chaos_step = chaos.step("routing") if chaos is not None and chaos.gain > 0.0 else 0
    jitter = chaos.routing_jitter([endpoint.name for endpoint in endpoints], step=chaos_step) if chaos is not None else {}
    if jitter:
        penalties = {name: min(1.0, value + jitter.get(name, 0.0)) for name, value in penalties.items()}
    # The learner (measured speed, Bayesian quality, dynamics modulation) replaces the table constants when a scope is active.
    from . import learner as learner_module  # local import: the learner reads this module lazily

    active_learner = learner_module.current()
    scores = active_learner.learned_scores(task_type, endpoints) if active_learner is not None else {}
    physics = active_learner.modulation(endpoints, usage) if active_learner is not None else {}
    utilities = {
        endpoint.name: _utility_score(
            endpoint, task_type, penalties[endpoint.name],
            speed=scores[endpoint.name].speed if scores else None,
            quality=scores[endpoint.name].quality_mean if scores else None,
            modulation=physics.get(endpoint.name, 0.0),
        )
        for endpoint in endpoints
    }
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
            keyed = [reasons for name, reasons in blocked.items() if enabled.get(name)]
            if keyed and all(any("daily cap" in reason for reason in reasons) for reasons in keyed):
                raise ProviderError(f"daily cap reached for every keyed vendor -> {detail}")
            raise ProviderError(f"Cortex 2 found no BYOK endpoint with headroom -> {detail}")
        # Equal utilities: the wave breaks the tie (lowest jitter first) instead of table order.
        chosen = max(feasible, key=lambda endpoint: (utilities[endpoint.name], -jitter.get(endpoint.name, 0.0), -endpoints.index(endpoint)))

    # Pink-wave exploration: on the steps the wave marks, a feasible runner-up inside the regret bound serves instead,
    # so the quality priors of the endpoints that rarely win keep being measured. The capacity rows are untouched.
    others = [endpoint for endpoint in feasible if endpoint.name != chosen.name]
    runner_up = max(others, key=lambda endpoint: (utilities[endpoint.name], -endpoints.index(endpoint))).name if others else ""
    explored = False
    explore_rate = 0.0
    winner_name = chosen.name
    if active_learner is not None and chaos is not None and jitter:
        explore_rate = active_learner.explore_rate(chaos.gain)
        if chaos.explore(chaos_step, explore_rate):
            alternative = active_learner.choose_exploration(feasible, utilities, chosen.name, task_type)
            if alternative is not None:
                chosen, explored, runner_up = alternative, True, winner_name
    decision_vector = {endpoint.name: int(endpoint.name == chosen.name) for endpoint in endpoints}
    chaos_note = f"; chaos={chaos.profile('routing')} jitter={jitter.get(chosen.name, 0.0):.4f}" if jitter else ""
    chaos_state = (
        {"gain": chaos.gain, "profile": chaos.profile("routing"), "step": chaos_step, "jitter": jitter.get(chosen.name, 0.0)} if jitter else {}
    )
    learned: Dict[str, Any] = {}
    if scores:
        score = scores[chosen.name]
        learned = {
            "speed": round(score.speed, 4), "p50_ms": score.p50_ms, "quality": round(score.quality_mean, 4), "quality_std": round(score.quality_std, 4),
            "modulation": physics.get(chosen.name, 0.0), "explore_rate": round(explore_rate, 4),
        }
    learned_note = f"; learner speed={learned['speed']} quality={learned['quality']} modulation={learned['modulation']:+.3f}" if learned else ""
    explored_note = f"; explored runner-up (rate {explore_rate:.3f}, winner was {winner_name})" if explored else ""
    return MILPDecision(
        endpoint=chosen,
        decision_vector=decision_vector,
        utility_scores=utilities,
        entropy_penalties=penalties,
        solver=solver_name,
        reason=(
            f"selected {chosen.name}; utility={utilities[chosen.name]:.4f}; "
            f"entropy_penalty={penalties[chosen.name]:.4f}; binary={decision_vector}{chaos_note}"
            + (f"; runner_up={runner_up}" if runner_up else "") + learned_note + explored_note
        ),
        runner_up=runner_up,
        chaos=chaos_state,
        explored=explored,
        learned=learned,
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


_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def vendor_wait_hint(headers: Mapping[str, Any]) -> float:
    """Seconds the vendor asks the app to wait, from Retry-After (seconds or an HTTP date) or a rate-limit reset header
    such as Groq's ``x-ratelimit-reset-requests: 2m59.56s``; 0 when the response carries no hint."""
    if not headers:
        return 0.0
    lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    hints = []
    retry_after = lowered.get("retry-after", "").strip()
    if retry_after:
        try:
            hints.append(float(retry_after))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime

                hints.append(parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "x-ratelimit-reset"):
        value = lowered.get(name, "").strip().lower()
        if not value:
            continue
        match = _DURATION_RE.fullmatch(value)
        if match and any(match.groups()):
            hours, minutes, seconds, millis = (float(group or 0.0) for group in match.groups())
            hints.append(hours * 3600.0 + minutes * 60.0 + seconds + millis / 1000.0)
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        hints.append(number - time.time() if number > 10_000_000 else number)  # an epoch or plain seconds
    return max(0.0, max(hints)) if hints else 0.0


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
    key_override = resolve_secret(selected.model_env).strip() if selected.model_env else ""  # session or environment pin
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
            response_headers = getattr(response, "headers", None) or {}
            retry_after = response_headers.get("Retry-After") if hasattr(response_headers, "get") else None
            response.close()
            if secret:
                detail = detail.replace(secret, "[REDACTED_SECRET]")
            last_error = f"HTTP {response.status_code}: {detail}"
            if int(response.status_code) == 429:
                # The vendor's own view of the window wins over the ledger's: the wait it names is recorded on the
                # bucket, so selection moves to another vendor now and the pacer knows the real wait. A long wait is
                # never slept through under the request lock.
                hint = vendor_wait_hint(response_headers) if hasattr(response_headers, "get") else 0.0
                if ledger is not None:
                    ledger.block(vendor, hint if hint > 0 else discovery.retry_delay(attempt))
                if hint > discovery.MAX_BACKOFF_SECONDS:
                    raise ProviderError(f"{selected.name} asked to wait {int(hint) + 1} s (HTTP 429: {detail[:120]})")
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
    if stream:
        payload["stream_options"] = {"include_usage": True}  # the last chunk then carries the vendor's own token count
    if selected.name == "groq" and "gpt-oss" in resolved_model and int(max_tokens) >= PAGE_OUTPUT_TOKENS:
        # gpt-oss spends the same ceiling on hidden reasoning; a page answer needs the ceiling for the page.
        payload["reasoning_effort"] = "low"
        payload["include_reasoning"] = False
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


def _extract_usage(selected: CortexEndpoint, payload: Mapping[str, Any]) -> int:
    """The vendor's total token count for the request when a chunk carries one (hidden reasoning included); 0 otherwise."""
    if selected.kind == "gemini":
        meta = payload.get("usageMetadata") or {}
        return int(meta.get("totalTokenCount") or 0) if isinstance(meta, dict) else 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        extra = payload.get("x_groq")
        usage = extra.get("usage") if isinstance(extra, dict) else None
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if total is None:
        total = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    return int(total or 0)


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

    ``status`` (when given) receives ``finish`` once the vendor reports why the answer ended, ``model`` (the id
    that actually served, after any rediscovery or sibling fallback), ``usage_tokens`` (the vendor's own count
    when its last chunk carries one) and ``elapsed_ms`` (from the POST to the end of the stream, no waits).

    Transient vendor errors are retried with backoff, a retired model id is
    replaced from the vendor's live list, and an overloaded model falls back
    to a sibling on the same vendor before the endpoint is declared failed.
    """
    if requests is None:
        raise ProviderError("requests is required for provider HTTP execution")
    selected = _as_endpoint(endpoint)
    effective_timeout = timeout or int(os.environ.get("CHAT_JOHNSON_TIMEOUT", "120"))
    started = time.monotonic()
    response, served_model = _resilient_post(
        selected, messages, max_tokens, temperature, stream=True, system_prompt=system_prompt,
        timeout=effective_timeout, ledger=ledger,
    )
    if status is not None:
        status["model"] = served_model
    emitted = False
    try:
        for payload_item in _iter_sse_payloads(response):
            finish = _extract_finish(selected, payload_item)
            if finish and status is not None:
                status["finish"] = finish
            if status is not None:
                usage_tokens = _extract_usage(selected, payload_item)
                if usage_tokens:
                    status["usage_tokens"] = usage_tokens
            chunk = _extract_stream_text(selected, payload_item)
            if chunk:
                emitted = True
                yield chunk
    except requests.RequestException as exc:  # a connection cut mid-answer is a provider failure, not a crash
        raise ProviderError(f"{selected.name} stream failed after {'some' if emitted else 'no'} text: {type(exc).__name__}") from exc
    finally:
        response.close()
        if status is not None:
            status["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if not emitted:
        raise ProviderError(f"{selected.name} returned an empty stream (no text; blocked, truncated, or filtered)")


_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def strip_reasoning_tags(text: str) -> str:
    """Remove <think>…</think> blocks reasoning models put in message content; never show hidden reasoning."""
    cleaned = _THINK_BLOCK.sub("", text or "")
    cleaned = _THINK_OPEN.sub("", cleaned)  # an unterminated block hides everything after it, as the live view does
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
    output_need: int = 0,
) -> Tuple[str, "RouteDecision"]:
    """Run Cortex 2 selection, then consume the Cortex 1 generation stream.

    Every endpoint failure is retained so the final error names each provider
    and its real HTTP status instead of a generic "no headroom" message.
    ``output_need`` excludes endpoints whose single-answer ceiling cannot hold the answer.
    """
    estimated_tokens = _estimate_tokens(messages) + max_tokens
    excluded: set[str] = cannot_write(output_need)
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
            raw = ""
            reservation = ledger.reserve(_vendor(endpoint), estimated_tokens) if ledger is not None else None
            try:
                for chunk in cortex_stream(
                    endpoint, messages, max_tokens=max_tokens, temperature=temperature, system_prompt=system_prompt, ledger=ledger, status=status,
                ):
                    raw += chunk
            finally:
                # The reservation becomes the real charge: the vendor's own count when its last chunk carried one, else
                # the raw text (hidden reasoning included); charged even when the stream failed part-way.
                if ledger is not None and reservation is not None:
                    ledger.settle(_vendor(endpoint), reservation, _charge_for(status, messages, raw), count_request=False)
            text = strip_reasoning_tags(raw)
            route_decision = RouteDecision(
                finish=str(status.get("finish", "")),
                provider=endpoint.name,
                model=str(status.get("model") or endpoint_model(endpoint)),
                task_type=task_type,
                reason=f"{decision.reason}; solver={decision.solver}; attempted={attempted}",
                solver=decision.solver,
                decision_vector=decision.decision_vector,
                runner_up=decision.runner_up,
                chaos=dict(decision.chaos),
                explored=decision.explored,
                elapsed_ms=int(status.get("elapsed_ms", 0) or 0),
            )
            return text, route_decision
        except ProviderError as exc:
            excluded.add(endpoint.name)
            failures[endpoint.name] = str(exc)
            _note_failure(endpoint.name, task_type)

    detail = "; ".join(f"{name}: {error}" for name, error in failures.items()) or "no keyed endpoint"
    raise ProviderError(f"all Cortex endpoints failed -> {detail}")


def _charge_for(status: Mapping[str, Any], messages: Sequence[Mapping[str, str]], raw: str) -> int:
    """What a finished (or broken) stream costs: the vendor's count when it gave one, else the estimate; 0 for no text."""
    counted = int(status.get("usage_tokens", 0) or 0)
    if counted > 0:
        return counted
    return _estimate_tokens(messages, raw) if raw else 0


def _note_failure(endpoint_name: str, task_type: str) -> None:
    """A failed send moves the endpoint's quality prior for that task (the learner only ever heard thumbs before)."""
    from . import learner as learner_module

    active = learner_module.current()
    if active is None:
        return
    try:
        learner_module.observe_outcome(active.project_scope, endpoint_name, task_type, "failed")
    except Exception:  # learning must never break a send
        pass


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
        output_need: int = 0,
    ) -> None:
        self.messages = [dict(message) for message in messages]
        self.ledger = ledger
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system_prompt = system_prompt
        estimated_tokens = _estimate_tokens(messages) + max_tokens
        self.milp = select_milp_endpoint(task_type, estimated_tokens, ledger=ledger, excluded=cannot_write(output_need))
        self.decision = RouteDecision(
            provider=self.milp.endpoint.name,
            model=endpoint_model(self.milp.endpoint),
            task_type=task_type,
            reason=f"{self.milp.reason}; solver={self.milp.solver}; streamed=True",
            solver=self.milp.solver,
            decision_vector=self.milp.decision_vector,
            runner_up=self.milp.runner_up,
            chaos=dict(self.milp.chaos),
            explored=self.milp.explored,
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
        vendor = _vendor(self.milp.endpoint)
        reservation = self.ledger.reserve(vendor, _estimate_tokens(self.messages) + int(self.max_tokens)) if self.ledger is not None else None
        try:
            yield from _visible_chunks(self._raw())
        except ProviderError:
            _note_failure(self.milp.endpoint.name, self.decision.task_type)
            raise
        finally:
            # The reservation becomes the real charge (the vendor's count, else the raw text), even after a broken stream.
            if self.ledger is not None and reservation is not None:
                self.ledger.settle(vendor, reservation, _charge_for(self.status, self.messages, self.text), count_request=False)
            self.text = strip_reasoning_tags(self.text)
            self.decision.finish = str(self.status.get("finish", ""))
            if self.status.get("model"):
                self.decision.model = str(self.status["model"])
            self.decision.elapsed_ms = int(self.status.get("elapsed_ms", 0) or 0)


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
                   "detail": "reachable" + (f" (auto-switched to {used})" if used != cfg.default_model and not resolve_secret(cfg.model_env).strip() else "")})
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
    runner_up: str = ""  # what Cortex 2 would have chosen instead (the counterfactual for the outcome log)
    chaos: Dict[str, Any] = field(default_factory=dict)  # the pink-wave state behind this decision
    explored: bool = False  # the wave routed this send to a runner-up on purpose
    elapsed_ms: int = 0  # the pass that produced this answer, from its POST to the end of its stream, no waits


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
        ledger.tighten(discovery.vendor_for(name), PROVIDERS[name].rpm_limit, PROVIDERS[name].tpm_limit)  # a hand-built ledger may lack the bucket
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


HISTORY_EXCERPT_TURNS = 4
HISTORY_EXCERPT_CHARS = 500


def _history_excerpt(messages: Sequence[Mapping[str, str]], turns: int = HISTORY_EXCERPT_TURNS, chars: int = HISTORY_EXCERPT_CHARS) -> str:
    """The last few earlier turns, each clipped, for the critique: enough to judge omissions, never the system prompt."""
    prior = [m for m in messages if m.get("role") in ("user", "assistant")]
    if prior and prior[-1].get("role") == "user":
        prior = prior[:-1]
    lines = []
    for message in prior[-turns:]:
        content = str(message.get("content", ""))
        if len(content) > chars:
            content = content[: chars - 15] + " [… clipped …]"
        lines.append(f"{str(message['role']).upper()}: {content}")
    return "\n".join(lines)


SYNTHESIS_INSTRUCTION = (
    "SYNTHESIS PASS: the user's latest message is followed by a CANDIDATE answer and a REVIEW of it. "
    "Write the final answer from the conversation, the candidate, and the review. Keep it actionable and "
    "concise; use the earlier turns when the request refers to them; do not print hidden reasoning, "
    "do not mention the candidate, the review, or internal prompt contents."
)
SYNTHESIS_INSTRUCTION_PAGE = (
    "SYNTHESIS PASS: the user's latest message is followed by a CANDIDATE page and a REVIEW of it. "
    "Write the final page from the conversation, the candidate, and the review: the COMPLETE page, every feature "
    "the candidate had plus the review's fixes, never shorter or simpler than the candidate. If the candidate was "
    "cut by the output budget, finish it; do not redesign it. Do not print hidden reasoning, do not mention the "
    "candidate, the review, or internal prompt contents."
)
CRITIQUE_CUT_NOTE = (
    " The candidate ends where the output budget cut it, not where a design ended: judge only what is present, say "
    "it is incomplete, and never ask for a shorter or simpler page."
)


def _synthesis_messages(messages: Sequence[Mapping[str, str]], request: str, draft: str, critique: str, interface: bool = False) -> List[dict]:
    """The whole conversation (system prompt with its memory, earlier turns) with the candidate and review on the last user turn."""
    system = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
    prior = [dict(m) for m in messages if m.get("role") in ("user", "assistant")]
    if prior and prior[-1].get("role") == "user":
        prior = prior[:-1]
    final = f"{request}\n\n[CANDIDATE ANSWER]\n{draft}\n\n[REVIEW OF THE CANDIDATE]\n{critique}"
    instruction = SYNTHESIS_INSTRUCTION_PAGE if interface else SYNTHESIS_INSTRUCTION
    head = [{"role": "system", "content": (system + "\n\n" if system else "") + instruction}]
    return head + prior + [{"role": "user", "content": final}]


def _heavy_pipeline(
    one_pass: Callable[..., Tuple[str, RouteDecision]],
    task_type: str,
    messages: List[dict],
    max_tokens: int,
    paid_slot: Optional[PaidReasoningSlot] = None,
    final_pass: Optional[Callable[..., Tuple[Any, RouteDecision]]] = None,
    temperatures: Optional[Tuple[float, float, float]] = None,
    pace: Optional[Callable[[List[dict], int], None]] = None,
    interface: bool = False,
) -> Tuple[Any, RouteDecision]:
    """Bounded plan/critique/synthesis without exposing private chain-of-thought.

    The optional paid slot, when armed for this session, handles only the
    critique pass; the draft and synthesis always run on free endpoints.
    ``final_pass`` may return an unexhausted stream instead of text so the UI
    can show the synthesis as it arrives; the fallbacks still return text.
    ``temperatures`` is the pink-wave schedule (draft, critique, synthesis);
    when None every pass keeps the caller's temperature (three-argument passes).
    ``pace`` runs before every free-endpoint pass with that pass's messages and
    token budget, so a pass waits for a free-tier window instead of failing.
    ``interface`` marks a page request: the draft gets the whole budget (a half-budget
    draft of a page is always cut, and a critique that judges the cut asks for a smaller
    page), the critique is told a cut is a cut, and the synthesis must not shorten.
    Every fallback carries the draft's ``finish`` so a cut draft is never stored as complete.
    """

    def run_pass(fn: Callable[..., Any], stage: int, selected_type: str, selected_messages: List[dict], tokens: int) -> Any:
        if pace is not None:
            pace(selected_messages, tokens)
        if temperatures is None:
            return fn(selected_type, selected_messages, tokens)
        return fn(selected_type, selected_messages, tokens, float(temperatures[stage]))

    draft_tokens = max_tokens if interface else max(256, max_tokens // 2)
    draft, draft_decision = run_pass(one_pass, 0, task_type, messages, draft_tokens)
    # The critique gets the request, the candidate, and a compact excerpt of the earlier turns (so
    # "omissions" is judged against what was actually discussed) but never the system prompt. The
    # synthesis writes the final answer, so it keeps the whole conversation: a synthesis that saw only
    # the request answered "I don't have access to previous code" whenever the request referred back.
    request = _last_user_turn(messages)
    critique_system = (
        "Review the candidate answer for correctness, omissions, unsafe assumptions, "
        "and concrete improvements. Check that it addresses the newest message first and then "
        "finishes any earlier unfinished request. When the request asks for an interface or page, "
        "check that the page is one complete self-contained ```html fence (inline CSS and JS, no "
        "external URLs, no data: link). Return a concise checklist only; do not reveal "
        "private chain-of-thought."
    )
    if draft_decision.finish == "length":
        critique_system += CRITIQUE_CUT_NOTE
    critique_messages = [
        {"role": "system", "content": critique_system},
        {"role": "user", "content": json.dumps({"request": request, "context": _history_excerpt(messages), "candidate": draft})},
    ]
    try:
        if paid_slot is not None and paid_slot.armed:
            critique, critique_decision = paid_slot_generate(paid_slot, critique_messages, max(256, max_tokens // 3))
        else:
            critique, critique_decision = run_pass(one_pass, 1, "reasoning", critique_messages, max(256, max_tokens // 3))
    except ProviderError:
        return draft, RouteDecision(
            draft_decision.provider,
            draft_decision.model,
            task_type,
            f"heavy draft only; critique unavailable; {draft_decision.reason}",
            draft_decision.solver,
            draft_decision.decision_vector,
            finish=draft_decision.finish,
        )

    synthesis_messages = _synthesis_messages(messages, request, draft, critique, interface=interface)
    try:
        final, final_decision = run_pass(final_pass or one_pass, 2, task_type, synthesis_messages, max_tokens)
    except ProviderError:
        return draft, RouteDecision(
            critique_decision.provider,
            critique_decision.model,
            task_type,
            f"heavy candidate returned because synthesis was unavailable; {critique_decision.reason}",
            critique_decision.solver,
            critique_decision.decision_vector,
            finish=draft_decision.finish,
        )
    final_decision.task_type = task_type
    schedule = f" temperatures={temperatures[0]}/{temperatures[1]}/{temperatures[2]};" if temperatures else ""
    final_decision.reason = (
        f"bounded heavy mode: draft -> review({critique_decision.provider}) -> synthesis;{schedule} "
        f"final={final_decision.reason}"
    )
    return final, final_decision


def _heavy_schedule(temperature: float) -> Optional[Tuple[float, float, float]]:
    """The active scope's pink-wave temperature schedule, or None when no wave is active or its gain is 0."""
    chaos = pinkwave.current()
    return chaos.heavy_schedule(float(temperature)) if chaos is not None else None


def heavy_stream(
    task_type: str,
    messages: List[dict],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
    paid_slot: Optional[PaidReasoningSlot] = None,
    interface: bool = False,
) -> Tuple[Any, RouteDecision]:
    """Heavy Mode with the synthesis streamed: draft and critique block, the final pass returns a CortexStream.

    The stream's ``text`` and ``decision.finish`` are complete only once it has been drained; when a
    fallback fires the first element is plain text instead. ``interface`` (a page request) routes the
    draft and synthesis only to endpoints that can write the whole page.
    """
    if not cortex_available():
        raise ProviderError("no Cortex endpoint is keyed; Heavy Mode streaming needs one")
    draft: Dict[str, Any] = {}

    def one_pass(selected_type: str, selected_messages: List[dict], tokens: int, pass_temperature: Optional[float] = None) -> Tuple[str, RouteDecision]:
        need = {"output_need": tokens} if (interface and selected_type != "reasoning") else {}
        text, decision = cortex_generate(
            selected_type, selected_messages, ledger=ledger, max_tokens=tokens,
            temperature=temperature if pass_temperature is None else pass_temperature, system_prompt=system_prompt, **need,
        )
        draft.setdefault("text", text)  # the first pass is the draft
        draft.setdefault("decision", decision)
        return text, decision

    def final_pass(selected_type: str, selected_messages: List[dict], tokens: int, pass_temperature: Optional[float] = None) -> Tuple[HeavyStream, RouteDecision]:
        stream = CortexStream(
            selected_type, selected_messages, ledger, max_tokens=tokens,
            temperature=temperature if pass_temperature is None else pass_temperature, system_prompt=system_prompt,
            **({"output_need": tokens} if interface else {}),
        )
        wrapped = HeavyStream(stream, str(draft.get("text", "")), draft.get("decision"), task_type)
        return wrapped, wrapped.decision

    return _heavy_pipeline(
        one_pass, task_type, messages, max_tokens, paid_slot=paid_slot, final_pass=final_pass,
        temperatures=_heavy_schedule(temperature), pace=_pacer(ledger), interface=interface,
    )


class HeavyStream:
    """The synthesis stream of Heavy Mode with the draft in hand: a stream that fails mid-flight yields the draft instead.

    ``text`` and ``decision`` are final once the stream is drained; the pipeline is never re-run.
    """

    def __init__(self, inner: CortexStream, draft: str, draft_decision: Optional[RouteDecision], task_type: str) -> None:
        self.inner = inner
        self.draft = draft
        self.draft_decision = draft_decision
        self.task_type = task_type
        self.decision = inner.decision
        self.text = ""
        self.fell_back = False

    def __iter__(self) -> Iterator[str]:
        emitted = False
        try:
            for chunk in self.inner:
                emitted = True
                yield chunk
            self.text = self.inner.text
        except ProviderError as exc:
            self.fell_back = True
            base = self.draft_decision or self.inner.decision
            if emitted and self.inner.text.strip():
                self.text = strip_reasoning_tags(self.inner.text)
                self.decision.reason = f"{self.decision.reason}; synthesis stream cut short: {str(exc)[:80]}"
                self.decision.finish = "length"
            else:
                self.text = self.draft
                self.decision = RouteDecision(
                    base.provider, base.model, self.task_type, f"heavy draft returned; synthesis stream failed: {str(exc)[:80]}",
                    base.solver, base.decision_vector, finish=getattr(base, "finish", ""),
                )
                yield self.draft


def generate_heavy(
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
    paid_slot: Optional[PaidReasoningSlot] = None,
    interface: bool = False,
) -> Tuple[str, RouteDecision]:
    """Run the legacy-provider bounded Heavy Mode pipeline."""
    return _heavy_pipeline(
        lambda selected_type, selected_messages, tokens, pass_temperature=None: generate(
            selected_type,
            selected_messages,
            ledger,
            max_tokens=tokens,
            temperature=temperature if pass_temperature is None else pass_temperature,
            settings=settings,
        ),
        task_type,
        messages,
        max_tokens,
        paid_slot=paid_slot,
        temperatures=_heavy_schedule(temperature),
        interface=interface,
    )


def generate_cortex_heavy(
    task_type: str,
    messages: List[dict],
    ledger: Optional[QuotaLedger] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    system_prompt: str = "",
    paid_slot: Optional[PaidReasoningSlot] = None,
    interface: bool = False,
) -> Tuple[str, RouteDecision]:
    """Run bounded Heavy Mode through the strict Cortex endpoint matrix."""
    return _heavy_pipeline(
        lambda selected_type, selected_messages, tokens, pass_temperature=None: cortex_generate(
            selected_type,
            selected_messages,
            ledger=ledger,
            max_tokens=tokens,
            temperature=temperature if pass_temperature is None else pass_temperature,
            system_prompt=system_prompt,
            **({"output_need": tokens} if (interface and selected_type != "reasoning") else {}),
        ),
        task_type,
        messages,
        max_tokens,
        paid_slot=paid_slot,
        temperatures=_heavy_schedule(temperature),
        pace=_pacer(ledger),
        interface=interface,
    )


def cortex_available() -> bool:
    """True when at least one strict Cortex endpoint has a BYOK key."""
    return any(_endpoint_key(endpoint) for endpoint in CORTEX_ENDPOINTS.values())


def cannot_write(output_need: int) -> set:
    """Endpoint names whose single-answer ceiling is below what this answer needs (empty when no need is stated)."""
    if int(output_need) <= 0:
        return set()
    return {name for name, endpoint in CORTEX_ENDPOINTS.items() if endpoint.max_output_tokens < int(output_need)}


def generate_mode(
    mode: str,
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
    paid_slot: Optional[PaidReasoningSlot] = None,
    interface: bool = False,
) -> Tuple[str, RouteDecision]:
    """Application entry point: Cortex endpoints first, legacy providers second.

    ``paid_slot`` is only consulted in Heavy Mode and only for the critique
    pass; Normal mode never touches it. ``interface`` marks a page request.
    """
    if cortex_available():
        try:
            if mode == "heavy":
                return generate_cortex_heavy(
                    task_type, messages, ledger, max_tokens, temperature, paid_slot=paid_slot, interface=interface
                )
            return cortex_generate(task_type, messages, ledger, max_tokens, temperature, output_need=max_tokens if interface else 0)
        except ProviderError as cortex_error:
            # A strict endpoint can be temporarily unavailable or have a
            # retired model id. Preserve the broader configured provider pool
            # as an explicit fallback rather than silently dropping the task.
            try:
                if mode == "heavy":
                    return generate_heavy(
                        task_type, messages, ledger, max_tokens, temperature, settings, paid_slot=paid_slot, interface=interface
                    )
                return generate(task_type, messages, ledger, max_tokens, temperature, settings)
            except ProviderError as legacy_error:
                raise ProviderError(
                    f"strict Cortex routing failed ({cortex_error}); "
                    f"legacy provider fallback failed ({legacy_error})"
                ) from legacy_error
    if mode == "heavy":
        return generate_heavy(task_type, messages, ledger, max_tokens, temperature, settings, paid_slot=paid_slot, interface=interface)
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
    was full (Gemini: a few requests per minute) instead of waiting, so multi-step runs failed from
    step two onward. This waits up to ``max_wait`` seconds, then uses Cortex routing with the
    legacy providers as fallback.
    """
    if ledger is not None:
        wait = cortex_wait_seconds(ledger, messages, max_tokens)
        if 0 < wait <= max_wait:
            time.sleep(wait + 0.5)
    ledger_value = ledger if ledger is not None else get_quota_ledger()
    return generate_mode("normal", task_type, messages, ledger_value, max_tokens=max_tokens, temperature=temperature)
