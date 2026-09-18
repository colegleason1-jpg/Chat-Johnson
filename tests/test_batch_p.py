"""Batch P: a full free-tier window is a wait, never a "no API key" error.

The pre-send wait ignores an endpoint that can never take the request, Heavy passes pace themselves,
the plain sentence explains the Cortex reason behind a fallback failure, and per-vendor RPM/TPM
ceilings can be overridden without a code change.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import errors, quota_registry, router  # noqa: E402
from orchestrator.router import CORTEX_ENDPOINTS, ProviderError, RouteDecision, _heavy_pipeline  # noqa: E402


@pytest.fixture()
def keyed(monkeypatch):
    for name in ("HF_TOKEN", "HUGGINGFACE_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    for name in ("CHAT_JOHNSON_RPM_GEMINI", "CHAT_JOHNSON_TPM_GEMINI", "CHAT_JOHNSON_RPM_GROQ", "CHAT_JOHNSON_TPM_GROQ"):
        monkeypatch.delenv(name, raising=False)
    quota_registry.reset_for_tests()
    yield
    quota_registry.reset_for_tests()


def test_wait_ignores_an_endpoint_that_can_never_take_the_request(keyed):
    ledger = quota_registry.get_quota_ledger()
    router._ensure_cortex_ledger(ledger)
    big = [{"role": "user", "content": "x" * 20_000}]  # ~5k tokens + 8192 output: over Groq's 8,000 TPM, inside Gemini's 32,000
    assert router.cortex_wait_seconds(ledger, big, 8192) == 0.0  # Gemini has room
    for _ in range(router.effective_rpm(CORTEX_ENDPOINTS["google_ai_studio"])):
        ledger.record_attempt("gemini")
    wait = router.cortex_wait_seconds(ledger, big, 8192)
    assert 0.0 < wait <= 60.0  # Groq's "no wait" no longer hides Gemini's real wait
    small = [{"role": "user", "content": "hello"}]
    assert router.cortex_wait_seconds(ledger, small, 512) == 0.0  # Groq can take a small request now
    # The retry helper: only a window failure earns a bounded wait; anything else, or a wait past the cap, returns 0.
    window = ProviderError("strict Cortex routing failed (Cortex 2 found no BYOK endpoint with headroom -> gemini: 5/5 requests used in the last minute; groq: request needs ~13192 tokens but the ceiling is 8000 TPM); legacy provider fallback failed (no legacy provider available -> no provider has a key)")
    assert abs(router.headroom_wait_seconds(window, ledger, big, 8192) - wait) < 1.0  # the window is measured live
    assert router.headroom_wait_seconds(window, ledger, big, 8192, max_wait=1.0) == 0.0
    assert router.headroom_wait_seconds(ProviderError("groq HTTP 401: bad key"), ledger, big, 8192) == 0.0


def test_the_plain_sentence_explains_the_cortex_reason_not_the_fallback():
    composite = ProviderError(
        "strict Cortex routing failed (Cortex 2 found no BYOK endpoint with headroom -> gemini: 5/5 requests used in the last minute; "
        "groq: request needs ~13192 tokens but the ceiling is 8000 TPM); legacy provider fallback failed (no legacy provider available -> no provider has a key)"
    )
    sentence = errors.plain_error(composite)
    assert "free-tier window" in sentence and "No API key" not in sentence
    too_big = ProviderError("strict Cortex routing failed (Cortex 2 found no BYOK endpoint with headroom -> groq: request needs ~13192 tokens but the ceiling is 8000 TPM); legacy provider fallback failed (no legacy provider available -> no provider has a key)")
    sentence = errors.plain_error(too_big)
    assert "lower the output token budget" in sentence and "13192" in sentence and "8000" in sentence
    no_key = ProviderError("strict Cortex routing failed (no BYOK key configured for groq); legacy provider fallback failed (no legacy provider available -> no provider has a key)")
    assert "paste a free-tier key" in errors.plain_error(no_key)
    inner_status = ProviderError("strict Cortex routing failed (groq HTTP 401: invalid api key); legacy provider fallback failed (no legacy provider available -> x)")
    assert "rejected the key" in errors.plain_error(inner_status)
    assert "No API key" in errors.plain_error(ProviderError("no legacy provider available -> no provider has a key"))  # no Cortex part: still the key sentence


def test_heavy_passes_pace_themselves_before_every_free_pass():
    paced, seen = [], []

    def one_pass(task_type, messages, tokens):
        seen.append(task_type)
        return f"{task_type}-answer", RouteDecision("free", "m", task_type, "r")

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    _heavy_pipeline(one_pass, "chat", messages, 900, pace=lambda m, t: paced.append((m[-1]["role"], t)))
    assert seen == ["chat", "reasoning", "chat"] and [t for _, t in paced] == [450, 300, 900] and all(r == "user" for r, _ in paced)
    assert router._pacer(None) is None


def test_heavy_stream_waits_for_a_window_between_passes(monkeypatch, keyed):
    from tests.test_batch_e import FakeStream

    slept, waits = [], iter([0.0, 12.0, 0.0])
    monkeypatch.setattr(router, "cortex_available", lambda: True)
    monkeypatch.setattr(router, "cortex_generate", lambda task_type, messages, ledger=None, max_tokens=4096, temperature=0.2, system_prompt="": (f"{task_type}-text", RouteDecision("free", "m", task_type, "r")))
    monkeypatch.setattr(router, "CortexStream", FakeStream)
    monkeypatch.setattr(router, "cortex_wait_seconds", lambda ledger, messages, max_tokens, output_need=0: next(waits))
    monkeypatch.setattr(router.time, "sleep", lambda seconds: slept.append(seconds))
    stream, decision = router.heavy_stream("chat", [{"role": "user", "content": "q"}], quota_registry.get_quota_ledger(), max_tokens=900)
    assert "".join(stream) == "final answer" and slept == [12.5]  # only the critique pass had to wait


def test_per_vendor_ceilings_can_be_overridden(monkeypatch, keyed):
    gemini, groq = CORTEX_ENDPOINTS["google_ai_studio"], CORTEX_ENDPOINTS["groq"]
    assert router.effective_rpm(gemini) == gemini.rpm_limit == 5 and router.effective_tpm(groq) == 8_000
    monkeypatch.setenv("CHAT_JOHNSON_RPM_GEMINI", "10")
    monkeypatch.setenv("CHAT_JOHNSON_TPM_GROQ", "0")
    assert router.effective_rpm(gemini) == 10 and router.effective_tpm(groq) is None
    assert router.EndpointUsage().rpm_ceiling(gemini) == 10.0 and router.EndpointUsage().tpm_ceiling(groq) is None
    monkeypatch.setenv("CHAT_JOHNSON_RPM_GEMINI", "nonsense")
    monkeypatch.setenv("CHAT_JOHNSON_TPM_GROQ", "300000")
    assert router.effective_rpm(gemini) == 5 and router.effective_tpm(groq) == 300_000
    assert router.prompt_context_chars(8192) == 24_000  # the widest keyed window now includes the raised Groq ceiling
