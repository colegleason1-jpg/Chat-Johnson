"""Batch B: daily caps, slim Heavy Mode payload, pytest gate, commit on green, snippet guard, plain errors, overrides."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import config, executor, patches, router, test_loop  # noqa: E402
from orchestrator.config import bind_session_keys  # noqa: E402
from orchestrator.errors import plain_error  # noqa: E402
from orchestrator.providers import ProviderError  # noqa: E402
from orchestrator.quota import QuotaLedger  # noqa: E402
from orchestrator.router import RouteDecision, _heavy_pipeline  # noqa: E402


# ---- B4 daily caps ---------------------------------------------------------------------------

def test_daily_cap_blocks_headroom_and_waits_for_the_day_to_roll():
    ledger = QuotaLedger({"fake": (100, 10_000)}, {"fake": 1_000})
    ledger.record("fake", 900)
    assert ledger.has_headroom("fake", 50)
    assert not ledger.has_headroom("fake", 200)
    assert ledger.wait_seconds("fake", 200) > 3600  # the day, not the minute
    use = ledger.usage("fake")
    assert use["daily_limit"] == 1_000 and use["daily_tokens"] == 900 and 0 < use["headroom"] <= 0.1
    ledger.set_daily_limit("fake", 0)
    assert ledger.has_headroom("fake", 200) and ledger.usage("fake")["daily_limit"] == 0


def test_daily_cap_table_and_env_override(monkeypatch):
    assert config.daily_cap("gemini") == config.DAILY_CAPS["gemini"] and config.daily_cap("unknown") == 0
    monkeypatch.setenv("CHAT_JOHNSON_DAILY_GEMINI", "12345")
    assert config.daily_cap("gemini") == 12345


# ---- B2 Heavy Mode payload -------------------------------------------------------------------

def test_heavy_mode_critique_sees_an_excerpt_and_the_synthesis_keeps_the_conversation():
    seen = []

    def one_pass(task_type, messages, tokens):
        seen.append((task_type, messages))
        return f"{task_type}-answer", RouteDecision("free", "m", task_type, "r")

    messages = [
        {"role": "system", "content": "SECRET SYSTEM PROMPT with the whole capability card"},
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "the actual request"},
    ]
    _heavy_pipeline(one_pass, "chat", messages, 900)
    assert seen[0][1] == messages  # the draft sees the full conversation
    critique = json.loads(seen[1][1][-1]["content"])
    assert "SECRET SYSTEM PROMPT" not in seen[1][1][-1]["content"] and "SECRET SYSTEM PROMPT" not in seen[1][1][0]["content"]
    assert critique["request"] == "the actual request" and "ASSISTANT: older answer" in critique["context"]
    # The synthesis writes the final answer, so it keeps the system prompt (memory) and the earlier turns.
    synthesis = seen[2][1]
    assert synthesis[0]["role"] == "system" and "SECRET SYSTEM PROMPT" in synthesis[0]["content"] and "SYNTHESIS PASS" in synthesis[0]["content"]
    assert [m["content"] for m in synthesis[1:-1]] == ["older question", "older answer"]
    assert synthesis[-1]["role"] == "user" and synthesis[-1]["content"].startswith("the actual request\n\n[CANDIDATE ANSWER]\nchat-answer")
    assert "[REVIEW OF THE CANDIDATE]\nreasoning-answer" in synthesis[-1]["content"]


# ---- B3 pytest gate and timeouts ------------------------------------------------------------

def test_repair_loop_with_zero_rounds_never_runs_pytest(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("pytest must not run when rounds are 0")

    monkeypatch.setattr(test_loop, "run_pytest", boom)
    passed, rounds, output = test_loop.repair_loop(str(tmp_path), "goal", QuotaLedger({}), None, max_rounds=0)
    assert passed and rounds == 0 and "not run" in output


def test_run_pytest_reports_a_timeout_instead_of_raising(monkeypatch, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(test_loop.subprocess, "run", slow)
    passed, output = test_loop.run_pytest(str(tmp_path), timeout=1)
    assert not passed and "timed out" in output


def test_apply_and_verify_commits_only_when_tests_pass(monkeypatch, tmp_path):
    commits = []
    monkeypatch.setattr(executor.sandbox, "commit_sandbox", lambda path, message: commits.append(message))
    monkeypatch.setattr(executor, "repair_loop", lambda *a, **k: (False, 1, "1 failed"))
    orch = executor.Orchestrator(settings=config.Settings(staging_root=str(tmp_path), max_test_rounds=1), ledger=QuotaLedger({}))
    note = orch._apply_and_verify("```file: a.py\nx = 1\n```", str(tmp_path), "goal", None, "fake")
    assert note.startswith("FAILED after 1 repair round") and commits == []
    monkeypatch.setattr(executor, "repair_loop", lambda *a, **k: (True, 0, "(tests not run: repair rounds set to 0; syntax guardrails only)"))
    note = orch._apply_and_verify("```file: a.py\nx = 2\n```", str(tmp_path), "goal", None, "fake")
    assert "tests not run" in note and len(commits) == 1


# ---- B5 snippet guard ----------------------------------------------------------------------

def test_snippet_bodies_are_rejected_and_complete_files_pass(tmp_path):
    (tmp_path / "mod.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\nclass C:\n    pass\n")
    blocks = {
        "mod.py": "def a():\n    return 10\n",  # one of three definitions, far shorter: an excerpt
        "other.py": "def a():\n    pass\n# ... rest of file unchanged\n",
        "new.py": "print('complete new file')\n",
        "README.md": "# Title\n...\nmore\n",  # a bare ellipsis line in prose counts as an elision too
    }
    accepted, rejected = patches.reject_snippets(str(tmp_path), blocks)
    assert set(accepted) == {"new.py"}
    assert any("mod.py" in r and "excerpt" in r for r in rejected)
    assert any("other.py" in r and "elides" in r for r in rejected)
    full = "def a():\n    return 10\n\n\ndef b():\n    return 20\n\n\nclass C:\n    x = 1\n"
    assert patches.looks_like_snippet("mod.py", full, (tmp_path / "mod.py").read_text()) == ""


# ---- B8 plain errors and overrides -----------------------------------------------------------

@pytest.mark.parametrize("exc, needle", [
    (ProviderError("groq HTTP 401: {\"error\": \"invalid api key\"}", status_code=401), "rejected the key"),
    (ProviderError("google_ai_studio HTTP 429: quota exceeded; retry in 12 s", status_code=429), "wait ~12 s"),
    (ProviderError("huggingface HTTP 404: model retired"), "no longer serves that model"),
    (ProviderError("no BYOK key configured for groq"), "paste a free-tier key"),
    (ProviderError("Cortex 2 found no BYOK endpoint with headroom -> gemini: 18.0s"), "free-tier window"),
    (ProviderError("groq HTTP 503: overloaded", status_code=503), "server error"),
    (RuntimeError("read timed out"), "did not answer in time"),
    (RuntimeError("something else entirely"), "something else entirely"),
])
def test_plain_error_maps_common_failures(exc, needle):
    assert needle in plain_error(exc)


def test_session_model_override_wins_over_environment_and_discovery(monkeypatch):
    monkeypatch.setenv("CORTEX_GROQ_MODEL", "env-model")
    endpoint = router.CORTEX_ENDPOINTS["groq"]
    assert router.endpoint_model(endpoint) == "env-model"
    bind_session_keys({"CORTEX_GROQ_MODEL": "session-model"})
    try:
        assert router.endpoint_model(endpoint) == "session-model"
        assert config.provider_model(config.PROVIDERS["groq"]) != "session-model"  # legacy table has its own env name
        bind_session_keys({"GROQ_MODEL": "legacy-session-model"})
        assert config.provider_model(config.PROVIDERS["groq"]) == "legacy-session-model"
    finally:
        bind_session_keys({})
