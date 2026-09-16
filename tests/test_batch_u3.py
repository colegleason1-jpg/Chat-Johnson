"""Batch U3: routing and quota that see reality.

The ledger reserves tokens while a request is in flight, honours the vendor's own wait (Retry-After and reset
headers), counts Gemini's requests per day, charges the vendor's token count when the last chunk carries one, records
the served model and the pass latency net of waits, folds cut and failed answers into the priors, scopes momentum per
workspace, runs background cycles in normal mode after a quiet period, and waits one window for the lock.
"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import discovery, errors, learner, pinkwave, quiet, quota_registry, router, vault  # noqa: E402
from orchestrator.quota import QuotaLedger  # noqa: E402
from orchestrator.router import CORTEX_ENDPOINTS, ProviderError, RouteDecision, cortex_generate  # noqa: E402
from orchestrator.society import academy, cycles, tick  # noqa: E402
from tests.test_cortex import FakeResponse, sse  # noqa: E402
from tests.test_cortex import all_keys as keys_fixture  # noqa: E402
from tests.test_batch_t_ui import app as app_fixture  # noqa: E402

app = app_fixture  # the AppTest fixture (tmp vault, no keys, no workers)
all_keys = keys_fixture  # the three fake vendor keys
assert all(getattr(module, "quiet", None) is quiet for module in (academy, cycles, tick))  # the cycles honour the quiet period


@pytest.fixture(autouse=True)
def quiet_network(monkeypatch):
    monkeypatch.setattr(discovery, "sleep", lambda seconds: None)
    monkeypatch.setattr(router.requests, "get", lambda *a, **k: FakeResponse(status_code=200, body={"data": [], "models": []}))
    router._DISCOVERED_MODELS.clear()
    yield
    router._DISCOVERED_MODELS.clear()


# ----------------------------------------------------------------------------
# Ledger
# ----------------------------------------------------------------------------

def test_a_reservation_holds_the_window_until_it_is_settled():
    ledger = QuotaLedger({"groq": (10, 1_000)})
    reservation = ledger.reserve("groq", 600)
    assert ledger.usage("groq")["tpm_used"] == 600 and ledger.usage("groq")["reserved"] == 600
    assert not ledger.has_headroom("groq", 500) and ledger.wait_seconds("groq", 500) > 0
    ledger.settle("groq", reservation, 250)
    use = ledger.usage("groq")
    assert use["tpm_used"] == 250 and use["reserved"] == 0 and use["rpm_used"] == 0  # settled without a request count
    empty = ledger.reserve("groq", 300)
    ledger.settle("groq", empty, 0)  # nothing came back: nothing is charged
    assert ledger.usage("groq")["tpm_used"] == 250


def test_a_vendor_block_is_the_real_wait():
    ledger = QuotaLedger({"gemini": (5, 32_000)})
    assert ledger.has_headroom("gemini", 10)
    ledger.block("gemini", 45)
    use = ledger.usage("gemini")
    assert not ledger.has_headroom("gemini", 10) and 44 <= ledger.wait_seconds("gemini", 10) <= 45
    assert 44 <= use["blocked_for"] <= 45 and use["headroom"] == 0.0


def test_a_daily_request_cap_counts_requests_not_tokens():
    ledger = QuotaLedger({"gemini": (5, 32_000)})
    ledger.tighten_daily_requests("gemini", 2)
    ledger.tighten_daily_requests("gemini", 3)  # never loosened
    assert ledger.daily_request_limit("gemini") == 2
    ledger.record_attempt("gemini")
    ledger.record("gemini", 5, count_request=True)
    use = ledger.usage("gemini")
    assert use["daily_requests"] == 2 and use["daily_request_limit"] == 2
    assert not ledger.has_headroom("gemini", 1) and ledger.wait_seconds("gemini", 1) > 60


def test_the_cortex_table_registers_gemini_requests_per_day(all_keys, monkeypatch):
    ledger = QuotaLedger({})
    router._ensure_cortex_ledger(ledger)
    assert ledger.daily_request_limit("gemini") == 200 and ledger.daily_request_limit("groq") == 0
    monkeypatch.setenv("CHAT_JOHNSON_RPD_GEMINI", "50")
    fresh = QuotaLedger({})
    router._ensure_cortex_ledger(fresh)
    assert fresh.daily_request_limit("gemini") == 50
    usage = router.EndpointUsage(daily_requests=50, daily_request_limit=50, blocked_for=12)
    reasons = router._capacity_reasons(CORTEX_ENDPOINTS["google_ai_studio"], usage, 100)
    assert any("50/50 requests used today" in r for r in reasons) and any("asked to wait 13 s" in r for r in reasons)


# ----------------------------------------------------------------------------
# The vendor's own wait
# ----------------------------------------------------------------------------

def test_vendor_wait_hint_reads_retry_after_and_reset_headers():
    assert router.vendor_wait_hint({"Retry-After": "60"}) == 60.0
    assert abs(router.vendor_wait_hint({"x-ratelimit-reset-requests": "2m59.56s"}) - 179.56) < 1e-6
    assert abs(router.vendor_wait_hint({"x-ratelimit-reset-tokens": "7.66s"}) - 7.66) < 1e-6
    assert router.vendor_wait_hint({"x-ratelimit-reset-tokens": "450ms"}) == 0.45
    assert router.vendor_wait_hint({}) == 0.0 and router.vendor_wait_hint({"Retry-After": "soon"}) == 0.0


def test_a_long_retry_after_blocks_the_vendor_and_moves_the_send(all_keys, monkeypatch):
    slept = []
    monkeypatch.setattr(discovery, "sleep", lambda seconds: slept.append(seconds))
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    monkeypatch.setattr(router, "project_seth_routing_entropy", lambda *a, **k: zero)
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        posts.append(url)
        if "generativelanguage" in url:
            response = FakeResponse(status_code=429, text="quota")
            response.headers = {"Retry-After": "60"}
            return response
        return FakeResponse(lines=sse({"choices": [{"delta": {"content": "from groq"}}]}))

    monkeypatch.setattr(router.requests, "post", fake_post)
    ledger = QuotaLedger({})
    text, decision = cortex_generate("context_load", [{"role": "user", "content": "x"}], ledger=ledger, max_tokens=16)
    assert text == "from groq" and decision.provider in ("groq", "huggingface")  # another keyed vendor took it at once
    assert sum(1 for url in posts if "generativelanguage" in url) == 1  # no retries slept through under the lock
    assert not any(s >= 60 for s in slept)
    assert 58 <= ledger.blocked_for("gemini") <= 60 and router.cortex_wait_seconds(ledger, [{"role": "user", "content": "x"}], 16) == 0.0
    plain = errors.plain_error(ProviderError("google_ai_studio asked to wait 61 s (HTTP 429: quota)"))
    assert plain.startswith("google_ai_studio asked the app to wait about 61 s")


# ----------------------------------------------------------------------------
# Usage, served model and pass latency
# ----------------------------------------------------------------------------

def test_the_vendor_count_and_served_model_are_recorded(all_keys, monkeypatch):
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    monkeypatch.setattr(router, "project_seth_routing_entropy", lambda *a, **k: zero)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen["payload"] = json
        return FakeResponse(lines=sse({"choices": [{"delta": {"content": "ok"}}]}, {"choices": [], "usage": {"total_tokens": 1234}}))

    monkeypatch.setattr(router.requests, "post", fake_post)
    ledger = QuotaLedger({})
    text, decision = cortex_generate("quick_text", [{"role": "user", "content": "x"}], ledger=ledger, max_tokens=16)
    assert text == "ok" and seen["payload"]["stream_options"] == {"include_usage": True}
    use = ledger.usage("groq")
    assert use["tpm_used"] == 1234 and use["reserved"] == 0 and use["rpm_used"] == 1
    assert decision.model == seen["payload"]["model"] and decision.elapsed_ms >= 0
    gemini = router._extract_usage(CORTEX_ENDPOINTS["google_ai_studio"], {"usageMetadata": {"totalTokenCount": 77, "thoughtsTokenCount": 40}})
    assert gemini == 77


def test_a_stream_settles_its_reservation_and_notes_a_failure(all_keys, monkeypatch):
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    monkeypatch.setattr(router, "project_seth_routing_entropy", lambda *a, **k: zero)
    monkeypatch.setattr(router.requests, "post", lambda *a, **k: FakeResponse(lines=sse({"choices": [{"delta": {"content": "partial"}}]}, {"error": {"message": "cut"}})))
    ledger = QuotaLedger({})
    stream = router.CortexStream("quick_text", [{"role": "user", "content": "x"}], ledger, max_tokens=16)
    with pytest.raises(ProviderError):
        list(stream)
    use = ledger.usage("groq")
    assert use["reserved"] == 0 and use["tpm_used"] >= 1  # the partial text was charged, the reservation released


# ----------------------------------------------------------------------------
# Learner
# ----------------------------------------------------------------------------

def test_cut_and_failed_answers_move_the_priors(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    assert learner.observe_outcome("u3", "groq", "chat", "cut")
    assert learner.observe_outcome("u3", "groq", "chat", "failed")
    alpha, beta, n = vault.quality_priors_for("u3")[("groq", "chat")]
    assert (alpha, beta, n) == (2.0, 3.5, 2)
    vault.record_route("u3", "normal_chat", "chat", "gemini/g", "normal", 900, "length", "r")
    assert learner.rebuild_priors("u3") == 1  # the cut row folds in as half a failure
    assert vault.quality_priors_for("u3")[("gemini", "chat")][1] == 2.5


def test_momentum_follows_the_sending_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    vault.record_route("u3-m", "company", "chat", "gemini/g", "normal", 500, "", "r")
    vault.record_route("u3-m", "normal_chat", "chat", "groq/x", "normal", 500, "", "r")
    vault.record_route("u3-m", "company", "chat", "failed", "normal", 500, "", "r")
    brain = learner.Learner("u3-m")
    learner.set_workspace("normal_chat")
    assert brain.momentum_endpoint() == "groq"
    learner.set_workspace("company")
    assert brain.momentum_endpoint() == "gemini"
    learner.set_workspace("academy")
    assert brain.momentum_endpoint() is None  # no history in that workspace: no inertia
    learner.set_workspace("")
    assert brain.momentum_endpoint() == "groq"


def test_exploration_only_picks_endpoints_the_rows_allow(all_keys, monkeypatch):
    brain = learner.Learner("u3-x", settings=learner.LearnerSettings())
    feasible = [CORTEX_ENDPOINTS["groq"]]  # gemini and hf blocked (cannot write the page, or no headroom)
    utilities = {"groq": 0.9, "google_ai_studio": 0.85, "huggingface": 0.8}
    assert brain.choose_exploration(feasible, utilities, "groq", "chat") is None


# ----------------------------------------------------------------------------
# Quiet period and the lock
# ----------------------------------------------------------------------------

def test_background_cycles_wait_for_the_quiet_period(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    now = 1_800_000_000.0
    assert quiet.quiet_seconds("u3-q", now) == 0.0
    quiet.note_chat_send("u3-q", page=False, now=now)
    assert quiet.quiet_seconds("u3-q", now + 10) == 290.0 and quiet.quiet_seconds("u3-q", now + 301) == 0.0
    quiet.note_chat_send("u3-q", page=True, now=now)
    assert quiet.quiet_seconds("u3-q", now + 10) == 590.0
    queued = []

    def enqueue(scope, kind, payload, secrets, run_after=0.0):
        queued.append((scope, kind, payload, run_after))
        return 42

    told = []
    result = quiet.deferral("u3-q", "company_cycle", {"company_id": 1, "chained_from": 7}, {}, 9, enqueue, lambda **f: told.append(f), now=now + 10)
    assert result["status"] == "deferred" and result["next_job"] == 42 and result["quiet_s"] == 591
    scope, kind, payload, run_after = queued[0]
    assert kind == "company_cycle" and payload == {"company_id": 1, "deferred_from": 9} and run_after == now + 600
    assert told and "operator just sent" in told[0]["text"]
    assert quiet.deferral("u3-q", "company_cycle", {}, {}, 9, enqueue, now=now + 700) is None


def test_the_chat_waits_one_window_for_the_lock():
    assert quota_registry.CHAT_LOCK_TIMEOUT_SECONDS == 65.0 and quota_registry.JOB_YIELD_SECONDS >= 65.0


# ----------------------------------------------------------------------------
# The app
# ----------------------------------------------------------------------------

def test_the_route_row_keeps_the_pass_latency_and_the_cut_moves_the_prior(app, monkeypatch):
    def answer(task_type, messages, *args, **kwargs):
        time.sleep(0.05)
        return "Half an answer", RouteDecision("fake", "m", task_type, "r", finish="length", elapsed_ms=1234)

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-u3")
    monkeypatch.setattr(router, "heavy_stream", answer)
    monkeypatch.setattr(router, "generate_mode", lambda mode, task_type, messages, *a, **k: answer(task_type, messages))
    monkeypatch.setattr(router, "cortex_wait_seconds", lambda *a, **k: 0.0)
    from orchestrator import proctor
    monkeypatch.setattr(proctor, "cached_fragility", lambda *a, **k: None)
    app.query_params["scope"] = "u3-app"
    app.query_params["ws"] = "normal_chat"
    app.run()
    app.chat_input[0].set_value("write a page with a button").run()
    assert not app.exception
    route = vault.recent_routes("u3-app", 1)[0]
    assert int(route["ms"]) == 1234 and route["finish"] == "length"
    assert vault.quality_priors_for("u3-app")[("fake", route["task_type"])][1] == 2.5
    stamp = json.loads(vault.setting_get("u3-app", quiet.SETTING))
    assert stamp["page"] is True and quiet.quiet_seconds("u3-app") > 500
    pinkwave.deactivate()
