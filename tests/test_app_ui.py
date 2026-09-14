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


SKIP_OLD = pytest.mark.skipif(Version(st.__version__) < Version("1.40"), reason="AppTest before 1.40 cannot read a radio that uses format_func")


def fake_github_get(url, headers=None, stream=False, timeout=None, allow_redirects=True):
    from tests.test_github_repo import FakeResponse, make_tarball
    if url.endswith("/user"):
        return FakeResponse(200, {"login": "me"})
    if "/user/repos" in url:
        return FakeResponse(200, [{"full_name": "me/proj"}, {"full_name": "me/other"}])
    if url.endswith("/repos/me/proj"):
        return FakeResponse(200, {"default_branch": "main"})
    if url.endswith("/commits/main"):
        return FakeResponse(200, {"sha": "abc1234def5678"})
    return FakeResponse(200, raw=make_tarball(evil=False))


def arm_and_connect(app, monkeypatch):
    from orchestrator import github_repo as gr
    monkeypatch.setattr(gr.requests, "get", fake_github_get)
    app.query_params["ws"] = "repository"
    app.run()
    app.checkbox(key="github_push_enabled").check().run()
    app.text_input(key="github_push_token").input("ghp_secret_token_123").run()
    assert any("Token armed" in i.value for i in app.info)
    assert any("Signed in as **me**" in c.value for c in app.caption)
    assert app.selectbox(key="repo_pick").value == "me/proj"  # listed automatically from the token
    next(b for b in app.button if b.label == "Connect me/proj").click().run()
    assert not app.exception
    assert any("Connected me/proj @ main (abc1234)" in s.value for s in app.success), [s.value for s in app.success]


@SKIP_OLD
def test_armed_github_push_sends_the_kit_and_lists_the_pull_request(app, monkeypatch):
    from orchestrator import github_push as gp
    from tests.test_github_push import make_fake

    calls = []
    monkeypatch.setattr(gp.requests, "request", make_fake(calls, existing=()))
    arm_and_connect(app, monkeypatch)
    assert app.session_state["repo_fetched"]["owner"] == "me"
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


@SKIP_OLD
def test_repository_work_has_four_tabs_directions_and_a_public_connect(app, monkeypatch):
    from orchestrator import github_repo as gr
    monkeypatch.setattr(gr.requests, "get", fake_github_get)
    app.query_params["ws"] = "repository"
    app.run()
    assert not app.exception
    assert len(app.tabs) >= 4  # Work, Deploy Kit, GitHub, Directions
    markdown = "\n".join(m.value for m in app.markdown)
    assert "What never happens here" in markdown and "Connect" in [b.label for b in app.button]
    assert any("No repository connected" in i.value for i in app.info)
    app.text_input(key="repo_manual").input("me/proj").run()
    next(b for b in app.button if b.label == "Connect me/proj").click().run()
    assert not app.exception
    assert any("Connected me/proj @ main (abc1234)" in s.value for s in app.success), [s.value for s in app.success]
    captions = "\n".join(c.value for c in app.caption)
    assert "Repository in context: me/proj@abc1234 · 2 files" in captions


@SKIP_OLD
def test_disconnect_clears_the_connection(app, monkeypatch):
    arm_and_connect(app, monkeypatch)
    next(b for b in app.button if b.label == "Disconnect").click().run()
    assert not app.exception
    assert "repo_fetched" not in app.session_state or not app.session_state["repo_fetched"]
    assert any("No repository connected" in i.value for i in app.info)


@SKIP_OLD
def test_empty_repository_connects_and_offers_the_first_commit(app, monkeypatch):
    from orchestrator import github_repo as gr
    from tests.test_github_repo import FakeResponse

    def fake_get(url, headers=None, stream=False, timeout=None, allow_redirects=True):
        if url.endswith("/user"):
            return FakeResponse(200, {"login": "me"})
        if "/user/repos" in url:
            return FakeResponse(200, [{"full_name": "me/blank"}])
        if url.endswith("/repos/me/blank"):
            return FakeResponse(200, {"default_branch": "main"})
        return FakeResponse(409, text='{"message":"Git Repository is empty."}')

    monkeypatch.setattr(gr.requests, "get", fake_get)
    app.query_params["ws"] = "repository"
    app.run()
    app.checkbox(key="github_push_enabled").check().run()
    app.text_input(key="github_push_token").input("ghp_secret_token_123").run()
    next(b for b in app.button if b.label == "Connect me/blank").click().run()
    assert not app.exception
    assert any("empty repository (no commits yet)" in s.value for s in app.success), [s.value for s in app.success]
    assert any("empty, nothing committed yet" in c.value for c in app.caption)
    next(b for b in app.button if b.label == "Generate kit").click().run()
    assert any(b.label.startswith("Create the first commit on main with") for b in app.button), [b.label for b in app.button]
