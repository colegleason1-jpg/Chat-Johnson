"""Mocked tests for the Tri-Processor Cortex. No network calls are made."""
import json
import math
import os
import sys
from typing import Any, Dict, List

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import router
from orchestrator.quota import QuotaLedger
from orchestrator.router import (
    CORTEX_ENDPOINTS,
    CortexStream,
    PaidReasoningSlot,
    ProviderError,
    RouteDecision,
    _heavy_pipeline,
    advance_stochastic_project_seth_step,
    append_system_prompt,
    build_cortex_request,
    cortex_generate,
    endpoint_model,
    generate_one_over_f_noise,
    paid_slot_generate,
    project_seth_routing_entropy,
    select_milp_endpoint,
    shannon_entropy,
)


@pytest.fixture()
def all_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-secret-key")
    monkeypatch.setenv("HF_TOKEN", "hf-secret-key")
    monkeypatch.delenv("HUGGINGFACE_API_KEY", raising=False)


class FakeResponse:
    def __init__(self, status_code: int = 200, lines: List[str] = (), text: str = "", body: Any = None):
        self.status_code = status_code
        self._lines = list(lines)
        self.text = text
        self._body = body
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def json(self):
        return self._body

    def close(self):
        self.closed = True


def sse(*chunks: Dict[str, Any]) -> List[str]:
    return [f"data: {json.dumps(chunk)}" for chunk in chunks] + ["data: [DONE]"]


# ----------------------------------------------------------------------------
# Cortex 3
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("alpha", [0.5, 1.0, 1.5])
def test_one_over_f_noise_recovers_alpha(alpha):
    result = generate_one_over_f_noise(4096, alpha=alpha, seed=11, sample_rate=100.0, low_frequency_hz=0.5)
    assert abs(float(np.mean(result.samples))) < 1e-9
    assert abs(float(np.std(result.samples)) - 1.0) < 1e-9
    assert float(result.power_spectrum[0]) < 1e-18  # DC bin removed
    assert abs(result.alpha_estimate - alpha) < 0.35
    assert 0.0 < result.entropy <= math.log2(128)


def test_one_over_f_noise_is_seeded_and_validates():
    a = generate_one_over_f_noise(256, seed=3)
    b = generate_one_over_f_noise(256, seed=3)
    assert np.array_equal(a.samples, b.samples)
    with pytest.raises(ValueError):
        generate_one_over_f_noise(2)
    with pytest.raises(ValueError):
        generate_one_over_f_noise(64, alpha=-1.0)


def test_sde_step_matches_closed_form():
    x, eta, dt, sigma, a, c = 0.3, -1.2, 0.01, 0.2, 0.9, 0.05
    expected = x + (a * (x - x**3) + c) * dt + sigma * (1 + abs(x)) * eta * math.sqrt(dt)
    assert advance_stochastic_project_seth_step(x, eta, dt, sigma, a, c) == pytest.approx(expected, abs=1e-15)
    with pytest.raises(ValueError):
        advance_stochastic_project_seth_step(x, eta, 0.0, sigma, a, c)


def test_shannon_entropy_bounds():
    assert shannon_entropy([1.0, 1.0, 1.0]) == 0.0
    assert shannon_entropy([]) == 0.0
    values = np.random.default_rng(1).normal(size=2048)
    assert 0.0 < shannon_entropy(values) <= math.log2(128)


def test_routing_entropy_is_normalized_per_channel():
    penalties = project_seth_routing_entropy(CORTEX_ENDPOINTS, seed=5)
    assert set(penalties) == set(CORTEX_ENDPOINTS)
    assert all(0.0 <= value <= 1.0 for value in penalties.values())
    assert penalties == project_seth_routing_entropy(CORTEX_ENDPOINTS, seed=5)


# ----------------------------------------------------------------------------
# Cortex 2
# ----------------------------------------------------------------------------

def test_milp_selects_exactly_one_and_respects_task_fit(all_keys):
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    for task, expected in (("context_load", "google_ai_studio"), ("quick_text", "groq"), ("code_patch", "groq")):
        decision = select_milp_endpoint(task, 2000, entropy_by_endpoint=zero)
        assert decision.endpoint.name == expected
        assert sum(decision.decision_vector.values()) == 1
        assert decision.solver == "scipy.optimize.milp"


def test_milp_tpm_ceiling_excludes_gemini(all_keys):
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    decision = select_milp_endpoint("context_load", 40_000, entropy_by_endpoint=zero)
    assert decision.endpoint.name != "google_ai_studio"
    assert decision.decision_vector["google_ai_studio"] == 0


def test_milp_rpm_exhaustion_moves_traffic(all_keys):
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    ledger = QuotaLedger({})
    router._ensure_cortex_ledger(ledger)
    ledger.record("groq", 10)
    for _ in range(29):
        ledger.record("groq", 10)
    decision = select_milp_endpoint("quick_text", 500, ledger=ledger, entropy_by_endpoint=zero)
    assert decision.endpoint.name != "groq"


def test_milp_missing_key_is_excluded(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "only-groq")
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    decision = select_milp_endpoint("context_load", 2000, entropy_by_endpoint=zero)
    assert decision.endpoint.name == "groq"


def test_milp_entropy_penalty_flips_close_calls_but_is_bounded(all_keys):
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    assert select_milp_endpoint("chat", 2000, entropy_by_endpoint=zero).endpoint.name == "groq"
    # A saturated penalty on the favourite moves a close call elsewhere...
    penalize_groq = {"google_ai_studio": 0.0, "groq": 1.0, "huggingface": 0.0}
    assert select_milp_endpoint("chat", 2000, entropy_by_endpoint=penalize_groq).endpoint.name != "groq"
    # ...but it is bounded (max 0.30) and cannot override a decisive task fit.
    penalize_gemini = {"google_ai_studio": 1.0, "groq": 0.0, "huggingface": 0.0}
    assert select_milp_endpoint("context_load", 2000, entropy_by_endpoint=penalize_gemini).endpoint.name == "google_ai_studio"


def test_milp_raises_when_nothing_feasible(monkeypatch):
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ProviderError):
        select_milp_endpoint("chat", 100)


def test_milp_fallback_without_scipy_matches(all_keys, monkeypatch):
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    with_scipy = select_milp_endpoint("context_load", 2000, entropy_by_endpoint=zero)
    monkeypatch.setattr(router, "milp", None)
    without = select_milp_endpoint("context_load", 2000, entropy_by_endpoint=zero)
    assert without.solver == "deterministic-binary-fallback"
    assert without.endpoint.name == with_scipy.endpoint.name


# ----------------------------------------------------------------------------
# Cortex 1
# ----------------------------------------------------------------------------

def test_append_system_prompt_merges_into_existing_system():
    messages = [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}]
    out = append_system_prompt(messages, "extra")
    assert out[0]["content"] == "base\n\nextra"
    assert messages[0]["content"] == "base"  # input untouched
    assert append_system_prompt([{"role": "user", "content": "hi"}], "x")[0] == {"role": "system", "content": "x"}


def test_build_request_gemini_and_openai_shapes(all_keys):
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
    url, headers, payload = build_cortex_request("google_ai_studio", messages, 100, 0.1, stream=True)
    assert url.endswith(":streamGenerateContent?alt=sse")
    assert headers["x-goog-api-key"] == "gemini-secret-key"
    assert payload["systemInstruction"]["parts"][0]["text"] == "sys"
    assert payload["contents"][0]["role"] == "user"

    url, headers, payload = build_cortex_request("groq", messages, 100, 0.1, stream=False)
    assert url.endswith("/chat/completions")
    assert headers["Authorization"] == "Bearer groq-secret-key"
    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["stream"] is False


def test_model_id_override_from_environment(all_keys, monkeypatch):
    monkeypatch.setenv("CORTEX_GEMINI_MODEL", "gemini-2.5-pro")
    assert endpoint_model(CORTEX_ENDPOINTS["google_ai_studio"]) == "gemini-2.5-pro"
    url, _, _ = build_cortex_request("google_ai_studio", [{"role": "user", "content": "x"}], 10, 0.1, stream=False)
    assert "/models/gemini-2.5-pro:" in url
    monkeypatch.delenv("CORTEX_GEMINI_MODEL")
    assert endpoint_model(CORTEX_ENDPOINTS["google_ai_studio"]) == "gemini-2.5-flash"


def test_cortex_generate_streams_records_ledger_and_reports_decision(all_keys, monkeypatch):
    calls: List[Dict[str, Any]] = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append({"url": url, "json": json})
        return FakeResponse(lines=sse(
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
        ))

    monkeypatch.setattr(router.requests, "post", fake_post)
    ledger = QuotaLedger({})
    text, decision = cortex_generate("quick_text", [{"role": "user", "content": "say hi"}], ledger=ledger, max_tokens=64)
    assert text == "Hello"
    assert decision.provider == "groq"
    assert decision.solver == "scipy.optimize.milp"
    assert ledger.usage("groq")["rpm_used"] == 1
    assert len(calls) == 1


def test_cortex_generate_falls_through_on_http_error_and_redacts(all_keys, monkeypatch):
    attempts: List[str] = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        attempts.append(url)
        if "groq" in url:
            return FakeResponse(status_code=500, text="boom groq-secret-key leaked")
        return FakeResponse(lines=sse({"choices": [{"delta": {"content": "ok"}}]}))

    monkeypatch.setattr(router.requests, "post", fake_post)
    text, decision = cortex_generate("quick_text", [{"role": "user", "content": "x"}], max_tokens=32)
    assert text == "ok"
    assert decision.provider != "groq"
    assert "attempted=['groq'" in decision.reason
    assert len(attempts) == 2


def test_cortex_stream_object_exposes_decision_before_iteration(all_keys, monkeypatch):
    monkeypatch.setattr(
        router.requests, "post",
        lambda *a, **k: FakeResponse(lines=sse({"choices": [{"delta": {"content": "a"}}]}, {"choices": [{"delta": {"content": "b"}}]})),
    )
    ledger = QuotaLedger({})
    stream = CortexStream("quick_text", [{"role": "user", "content": "x"}], ledger=ledger, max_tokens=16)
    assert isinstance(stream.decision, RouteDecision)
    assert stream.decision.provider == "groq"
    assert "".join(stream) == "ab"
    assert stream.text == "ab"
    assert ledger.usage("groq")["rpm_used"] == 1


def test_http_error_message_never_contains_secret(all_keys, monkeypatch):
    monkeypatch.setattr(router.requests, "post", lambda *a, **k: FakeResponse(status_code=401, text="bad key groq-secret-key"))
    with pytest.raises(ProviderError) as excinfo:
        list(router.cortex_stream("groq", [{"role": "user", "content": "x"}]))
    assert "groq-secret-key" not in str(excinfo.value)
    assert "[REDACTED_SECRET]" in str(excinfo.value)


# ----------------------------------------------------------------------------
# Heavy Mode and the paid slot
# ----------------------------------------------------------------------------

def make_one_pass(log: List[str], fail_on: str = ""):
    def one_pass(task_type, messages, tokens):
        log.append(task_type)
        if fail_on and task_type == fail_on and log.count(fail_on) == 1:
            raise ProviderError("simulated outage")
        return f"{task_type}-answer", RouteDecision("free", "m", task_type, "r")
    return one_pass


def test_heavy_pipeline_is_three_bounded_passes():
    log: List[str] = []
    text, decision = _heavy_pipeline(make_one_pass(log), "chat", [{"role": "user", "content": "q"}], 900)
    assert log == ["chat", "reasoning", "chat"]
    assert text == "chat-answer"
    assert "draft -> review(free) -> synthesis" in decision.reason


def test_heavy_pipeline_degrades_to_draft_when_critique_fails():
    log: List[str] = []
    text, decision = _heavy_pipeline(make_one_pass(log, fail_on="reasoning"), "chat", [{"role": "user", "content": "q"}], 900)
    assert text == "chat-answer"
    assert "critique unavailable" in decision.reason
    assert log == ["chat", "reasoning"]


def test_paid_slot_is_never_armed_by_default_or_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "should-be-ignored")
    slot = PaidReasoningSlot()
    assert not slot.armed
    assert slot.status()["key_present"] is False
    slot.api_key = "sk-session-only"
    assert not slot.armed  # key alone is not enough; the toggle must be on
    slot.enabled = True
    assert slot.armed
    assert "sk-session-only" not in json.dumps(slot.status())
    with pytest.raises(ProviderError):
        paid_slot_generate(PaidReasoningSlot(api_key="k", enabled=False), [{"role": "user", "content": "x"}])


def test_paid_slot_handles_only_the_critique_pass(monkeypatch):
    seen: List[Dict[str, Any]] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(body={"choices": [{"message": {"content": "review-list"}}]})

    monkeypatch.setattr(router.requests, "post", fake_post)
    log: List[str] = []
    slot = PaidReasoningSlot(api_key="sk-session", model="o3-mini", enabled=True)
    text, decision = _heavy_pipeline(make_one_pass(log), "chat", [{"role": "user", "content": "q"}], 900, paid_slot=slot)
    assert log == ["chat", "chat"]  # free draft + free synthesis; critique went to the paid slot
    assert len(seen) == 1
    assert seen[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert "max_completion_tokens" in seen[0]["json"] and "temperature" not in seen[0]["json"]
    assert "review(paid_slot)" in decision.reason


def test_normal_mode_never_touches_paid_slot(all_keys, monkeypatch):
    urls: List[str] = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        urls.append(url)
        return FakeResponse(lines=sse({"choices": [{"delta": {"content": "x"}}]}))

    monkeypatch.setattr(router.requests, "post", fake_post)
    slot = PaidReasoningSlot(api_key="sk-session", enabled=True)
    router.generate_mode("normal", "quick_text", [{"role": "user", "content": "q"}], QuotaLedger({}), max_tokens=16, paid_slot=slot)
    assert all("openai.com" not in url for url in urls)


# ----------------------------------------------------------------------------
# Error surfacing and connection probe
# ----------------------------------------------------------------------------

def test_cortex_generate_reports_every_real_failure(all_keys, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        if "groq" in url:
            return FakeResponse(status_code=401, text="invalid api key")
        if "googleapis" in url:
            return FakeResponse(status_code=404, text="model not found")
        return FakeResponse(status_code=503, text="hf busy")

    monkeypatch.setattr(router.requests, "post", fake_post)
    with pytest.raises(ProviderError) as excinfo:
        cortex_generate("quick_text", [{"role": "user", "content": "hi"}], max_tokens=16)
    message = str(excinfo.value)
    assert "groq: groq HTTP 401" in message
    assert "google_ai_studio: google_ai_studio HTTP 404" in message
    assert "huggingface: huggingface HTTP 503" in message
    assert "no BYOK endpoint" not in message


def test_legacy_generate_reports_every_real_failure(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for name in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def fake_chat(provider, messages, max_tokens, temperature, settings):
        raise ProviderError(f"HTTP 401 from {provider}")

    monkeypatch.setattr(router, "chat", fake_chat)
    with pytest.raises(ProviderError) as excinfo:
        router.generate("chat", [{"role": "user", "content": "hi"}], QuotaLedger({n: (30, 100000) for n in router.PROVIDERS}), max_tokens=16)
    message = str(excinfo.value)
    assert "groq: HTTP 401 from groq" in message
    assert "gemini: HTTP 401 from gemini" in message


def test_probe_reports_status_without_leaking_key(all_keys, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        assert json.get("max_tokens") == 8 or json.get("generationConfig", {}).get("maxOutputTokens") == 8
        if "groq" in url:
            return FakeResponse(status_code=200, text="{}")
        if "googleapis" in url:
            return FakeResponse(status_code=404, text="gemini-secret-key model gone")
        return FakeResponse(status_code=401, text="nope")

    monkeypatch.setattr(router.requests, "post", fake_post)
    results = {row["endpoint"]: row for row in router.probe_all_endpoints()}
    assert results["groq"]["ok"] is True and results["groq"]["status"] == 200
    assert results["google_ai_studio"]["ok"] is False and results["google_ai_studio"]["status"] == 404
    assert "CORTEX_GEMINI_MODEL" in results["google_ai_studio"]["detail"]
    assert "gemini-secret-key" not in str(results)
    assert "key rejected" in results["huggingface"]["detail"]


def test_probe_without_key_does_not_call_network(monkeypatch):
    for name in ("GROQ_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(router.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    row = router.probe_endpoint("groq")
    assert row["ok"] is False and row["detail"] == "no key configured"


# ----------------------------------------------------------------------------
# Live model discovery after a vendor retirement
# ----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_discovery():
    router._DISCOVERED_MODELS.clear()
    yield
    router._DISCOVERED_MODELS.clear()


class FakeGet:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_retired_groq_model_is_discovered_and_retried(all_keys, monkeypatch):
    posts: List[str] = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        posts.append(json["model"])
        if json["model"] == "openai/gpt-oss-120b":
            return FakeResponse(lines=sse({"choices": [{"delta": {"content": "alive"}}]}))
        return FakeResponse(status_code=400, text='{"error":{"message":"The model `x` has been decommissioned","code":"model_decommissioned"}}')

    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeGet(200, {"data": [{"id": "openai/gpt-oss-20b"}, {"id": "openai/gpt-oss-120b"}]})

    monkeypatch.setenv("CORTEX_GROQ_MODEL", "")
    monkeypatch.setattr(router.requests, "post", fake_post)
    monkeypatch.setattr(router.requests, "get", fake_get)
    # Force the default to a retired id to simulate a stale deployment.
    stale = router.CORTEX_ENDPOINTS["groq"]
    monkeypatch.setitem(router.CORTEX_ENDPOINTS, "groq", router.CortexEndpoint(**{**stale.__dict__, "model": "llama-3.3-70b-versatile"}))
    text = "".join(router.cortex_stream("groq", [{"role": "user", "content": "x"}]))
    assert text == "alive"
    assert posts == ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]
    assert endpoint_model(router.CORTEX_ENDPOINTS["groq"]) == "openai/gpt-oss-120b"


def test_gemini_discovery_prefers_newest_flash(all_keys, monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeGet(200, {"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
        ]})

    monkeypatch.setattr(router.requests, "get", fake_get)
    assert router.discover_endpoint_model("google_ai_studio") == "gemini-3.5-flash"
    assert endpoint_model(router.CORTEX_ENDPOINTS["google_ai_studio"]) == "gemini-3.5-flash"


def test_env_override_beats_discovery(all_keys, monkeypatch):
    router._DISCOVERED_MODELS["groq"] = "discovered-id"
    monkeypatch.setenv("CORTEX_GROQ_MODEL", "pinned-id")
    assert endpoint_model(router.CORTEX_ENDPOINTS["groq"]) == "pinned-id"


def test_non_retirement_errors_do_not_trigger_discovery(all_keys, monkeypatch):
    monkeypatch.setattr(router.requests, "post", lambda *a, **k: FakeResponse(status_code=401, text="bad key"))
    monkeypatch.setattr(router.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no discovery")))
    with pytest.raises(ProviderError):
        list(router.cortex_stream("groq", [{"role": "user", "content": "x"}]))


def test_probe_reports_auto_switch(all_keys, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if json.get("model") == "openai/gpt-oss-120b":
            return FakeResponse(status_code=200, text="{}")
        return FakeResponse(status_code=404, text="model not found")

    monkeypatch.setattr(router.requests, "post", fake_post)
    monkeypatch.setattr(router.requests, "get", lambda *a, **k: FakeGet(200, {"data": [{"id": "openai/gpt-oss-120b"}]}))
    stale = router.CORTEX_ENDPOINTS["groq"]
    monkeypatch.setitem(router.CORTEX_ENDPOINTS, "groq", router.CortexEndpoint(**{**stale.__dict__, "model": "dead-model"}))
    row = router.probe_endpoint("groq")
    assert row["ok"] is True
    assert row["model"] == "openai/gpt-oss-120b"
    assert "auto-switched from retired dead-model" in row["detail"]
