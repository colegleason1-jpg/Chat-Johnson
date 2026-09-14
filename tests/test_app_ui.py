"""In-process UI checks with Streamlit's AppTest: no browser, no network, no keys."""
import json
import os
import sys

import pytest
import streamlit as st
from packaging.version import Version
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

KEY_ENVS = ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.setenv("CHAT_JOHNSON_BUILD", "abc1234")
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delattr(st, "segmented_control", raising=False)  # AppTest has no element for it; the radio path is exercised
    return AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)


def test_default_workspace_is_normal_chat_with_a_disabled_bar(app):
    app.run()
    assert not app.exception
    assert app.session_state["workspace_select"] == "normal_chat"
    bar = app.chat_input[0]
    assert bar.proto.disabled is True and "API key" in bar.proto.placeholder


def test_health_view_prints_json_and_stops(app):
    app.query_params["health"] = "1"
    app.run()
    assert not app.exception
    payload = json.loads(app.code[0].value)
    assert payload["status"] == "ok" and payload["build"] == "abc1234" and payload["vault"]["ok"] is True
    assert not app.sidebar.title  # the page stopped before the control deck rendered


@pytest.mark.skipif(Version(st.__version__) < Version("1.40"), reason="AppTest before 1.40 cannot read a radio that uses format_func")
def test_armed_github_push_sends_the_kit_and_lists_the_pull_request(app, monkeypatch):
    from orchestrator import github_push as gp
    from tests.test_github_push import make_fake

    calls = []
    monkeypatch.setattr(gp.requests, "request", make_fake(calls, existing=()))
    app.query_params["ws"] = "repository"
    app.run()
    assert not app.exception
    assert app.session_state["workspace_select"] == "repository"
    app.checkbox(key="github_push_enabled").check().run()
    app.text_input(key="github_push_repo").input("me/proj").run()
    app.text_input(key="github_push_token").input("ghp_secret_token_123").run()
    assert any("ARMED: pushes go to me/proj" in w.value for w in app.warning)
    generate = next(b for b in app.button if b.label == "Generate kit")
    generate.click().run()
    assert not app.exception
    assert any("file(s) for" in s.value and "0 error(s)" in s.value for s in app.success)
    push = next(b for b in app.button if b.label.startswith("Push ") and "pull request" in b.label)
    push.click().run()
    assert not app.exception
    messages = [s.value for s in app.success]
    assert any("opened pull request #101" in m for m in messages), messages
    assert "ghp_secret_token_123" not in "\n".join(messages)
    assert any(p.endswith("/pulls") for _, p, _ in calls)


@pytest.mark.skipif(Version(st.__version__) < Version("1.40"), reason="AppTest before 1.40 cannot read a radio that uses format_func")
def test_repository_work_has_four_tabs_directions_and_a_working_fetch(app, monkeypatch):
    from orchestrator import github_repo as gr
    from tests.test_github_repo import FakeResponse, make_tarball

    def fake_get(url, headers=None, stream=False, timeout=None, allow_redirects=True):
        if url.endswith("/repos/me/proj"):
            return FakeResponse(200, {"default_branch": "main"})
        if url.endswith("/commits/main"):
            return FakeResponse(200, {"sha": "abc1234def5678"})
        return FakeResponse(200, raw=make_tarball(evil=False))

    monkeypatch.setattr(gr.requests, "get", fake_get)
    app.query_params["ws"] = "repository"
    app.run()
    assert not app.exception
    assert len(app.tabs) >= 4  # Work, Deploy Kit, GitHub, Directions
    markdown = "\n".join(m.value for m in app.markdown)
    assert "What never happens here" in markdown and "Fetch repository" in [b.label for b in app.button]
    app.text_input(key="repo_fetch_repo").input("me/proj").run()
    next(b for b in app.button if b.label == "Fetch repository").click().run()
    assert not app.exception
    assert any("Fetched me/proj @ main (abc1234)" in s.value for s in app.success), [s.value for s in app.success]
    captions = "\n".join(c.value for c in app.caption)
    assert "Repository in context: me/proj@abc1234 · 2 files" in captions


def test_repository_chat_says_when_nothing_is_loaded(app):
    app.query_params["ws"] = "repository"
    app.run()
    assert not app.exception
    assert any("No repository loaded" in i.value for i in app.info)
