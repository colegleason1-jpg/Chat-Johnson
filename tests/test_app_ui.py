"""In-process UI checks with Streamlit's AppTest: no browser, no network, no keys."""
import json
import os
import sys

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

KEY_ENVS = ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.setenv("CHAT_JOHNSON_BUILD", "abc1234")
    monkeypatch.setenv("CHAT_JOHNSON_JOB_WORKERS", "0")  # rows are seeded by hand; no worker thread claims them
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


def test_disconnect_clears_the_connection(app, monkeypatch):
    arm_and_connect(app, monkeypatch)
    next(b for b in app.button if b.label == "Disconnect").click().run()
    assert not app.exception
    assert "repo_fetched" not in app.session_state or not app.session_state["repo_fetched"]
    assert any("No repository connected" in i.value for i in app.info)


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


def test_scope_is_private_per_visitor_and_follows_the_url(app):
    app.run()
    assert not app.exception
    minted = app.session_state["project_scope"]
    assert minted.startswith("visitor-") and len(minted) == len("visitor-") + 12
    app.query_params["scope"] = "visitor-abc123def456"
    app.session_state["project_scope"] = "visitor-abc123def456"  # what a reload with ?scope= restores
    app.run()
    assert app.session_state["project_scope"] == "visitor-abc123def456"
    assert any("Private scope `visitor-abc123def456`" in c.value for c in app.sidebar.caption)


def test_jobs_strip_shows_a_waiting_question_and_sends_the_answer(app):
    from orchestrator import vault
    vault.initialize_database()
    job_id = vault.enqueue_job("visitor-test", "mission", {"goal": "write a memo"}, thread_id=None)
    assert vault.claim_job("tester", ("mission",))["id"] == job_id
    vault.ask_job_question(job_id, "Which tone should the memo use?")
    app.query_params["scope"] = "visitor-test"
    app.run()
    assert not app.exception
    assert any("Which tone should the memo use?" in i.value for i in app.info)
    assert any(b.label == "Cancel" for b in app.button)
    app.text_input(key=f"job_answer_{job_id}").input("formal").run()
    next(b for b in app.button if b.label == "Send answer").click().run()
    assert not app.exception
    row = vault.job_by_id(job_id)
    assert row["status"] == "running" and row["answer"] == "formal"
    # Without an active job the strip disappears.
    vault.finish_job(job_id, "done", {"succeeded": 1, "failed": 0, "failures": []})
    app.run()
    assert not any(b.label == "Send answer" for b in app.button)


def test_launch_queues_the_mission_as_a_job_and_shows_the_strip(app, monkeypatch):
    from orchestrator import jobs, vault
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-the-launch-button")
    app.query_params["scope"] = "visitor-launch"
    app.query_params["ws"] = "task_finder"
    app.run()
    assert not app.exception
    app.chat_input[0].set_value("Research the history of tidal power and summarise it").run()
    assert not app.exception
    launch = next(b for b in app.button if b.label.startswith("Launch workstreams"))
    launch.click().run()
    assert not app.exception
    rows = vault.list_jobs("visitor-launch", kind="mission")
    assert len(rows) == 1 and rows[0]["status"] == "queued"  # no worker in tests, so it waits in the queue
    job = vault.job_view(rows[0])
    assert job["payload"]["goal"].startswith("Research the history") and len(job["payload"]["plan"]) >= 1
    assert job["payload"]["mode"] == "normal" and "AIza" not in rows[0]["payload"]
    assert jobs.secrets_held(job["id"]) is False or jobs._SECRETS[job["id"]] == {}  # no session keys were pasted; env keys stay in the environment
    assert any("Mission #" in m.value and "queued" in m.value for m in app.markdown)
    assert any("Mission running in the background" in c.value for c in app.caption)
    assert vault.recent_messages("visitor-launch", 10, workspace="task_finder")[0]["content"].startswith("MISSION: ")


def test_sidebar_has_key_guides_overrides_and_no_oauth(app):
    app.run()
    assert not app.exception
    labels = [e.label for e in app.sidebar.expander]
    assert any("How to get a free key" in label for label in labels)
    assert any("Model overrides" in label for label in labels)
    assert not any("GitHub identity" in label for label in labels)
    app.text_input(key="model_override_CORTEX_GROQ_MODEL").input("llama-custom").run()
    next(b for b in app.button if b.label == "Apply overrides").click().run()
    assert app.session_state["byok_keys"].get("CORTEX_GROQ_MODEL") == "llama-custom"
    assert any("Model overrides applied" in s.value for s in app.success)


def test_company_workspace_seeds_and_queues_a_cycle(app, monkeypatch):
    from orchestrator import vault
    from orchestrator.society import store
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key")
    app.query_params["scope"] = "visitor-company"
    app.query_params["ws"] = "company"
    app.run()
    assert not app.exception
    next(b for b in app.button if b.label == "Create AVS Studio").click().run()
    assert not app.exception
    assert len(store.seats_for("visitor-company", store.company_by_key("visitor-company", "avs_studio")["id"])) == 23
    assert any("23 seats" in c.value for c in app.caption)
    next(b for b in app.button if b.label == "Run a cycle now").click().run()
    assert not app.exception
    rows = vault.list_jobs("visitor-company", ("queued",), kind="company_cycle")
    assert len(rows) == 1 and vault.job_view(rows[0])["payload"]["company_id"] == store.company_by_key("visitor-company", "avs_studio")["id"]
    assert any("Company cycle #" in m.value for m in app.markdown)  # the jobs strip shows it


def test_academy_workspace_seeds_and_queues_a_cycle(app, monkeypatch):
    from orchestrator import vault
    from orchestrator.society import store
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key")
    app.query_params["scope"] = "visitor-academy"
    app.query_params["ws"] = "academy"
    app.run()
    assert not app.exception
    next(b for b in app.button if b.label.startswith("Seed the society to")).click().run()
    assert not app.exception
    assert len(store.agents_for("visitor-academy")) == 100
    next(b for b in app.button if b.label == "Run an academy cycle now").click().run()
    assert not app.exception
    rows = vault.list_jobs("visitor-academy", ("queued",), kind="academy_cycle")
    assert len(rows) == 1 and any("Academy cycle #" in m.value for m in app.markdown)


def test_academy_workspace_starts_and_stops_the_society_tick(app, monkeypatch):
    from orchestrator import vault
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key")
    app.query_params["scope"] = "visitor-tick"
    app.query_params["ws"] = "academy"
    app.run()
    next(b for b in app.button if b.label == "Start the society tick").click().run()
    assert not app.exception
    rows = vault.list_jobs("visitor-tick", ("queued",), kind="society_tick")
    assert len(rows) == 1 and vault.job_view(rows[0])["payload"]["interval_s"] == 1800.0
    assert any("Running · next tick" in c.value for c in app.caption)
    next(b for b in app.button if b.label == "Stop the tick").click().run()
    assert not app.exception
    assert vault.job_by_id(rows[0]["id"])["status"] == "cancelled"


def test_sidebar_shows_a_local_model_registered_from_the_environment(app, monkeypatch):
    from orchestrator import router
    monkeypatch.setenv("CHAT_JOHNSON_LOCAL_ENDPOINT", "http://ollama:11434/v1")
    monkeypatch.setenv("CHAT_JOHNSON_LOCAL_MODEL", "llama3.1:8b")
    try:
        app.run()
        assert not app.exception
        assert any("Local model (self-hosted)" in m.value for m in app.sidebar.markdown)
        assert any("the academy's cheap labour goes here first" in c.value for c in app.sidebar.caption)
    finally:
        router.unregister_local_endpoint()
        monkeypatch.delenv("CHAT_JOHNSON_LOCAL_KEY", raising=False)


def test_navigator_focus_shows_the_window_around_a_message(app):
    from orchestrator import vault
    app.query_params["scope"] = "visitor-nav"
    app.run()
    thread = int(vault.active_thread("visitor-nav", "normal_chat")["id"])
    ids = [vault.append_message("visitor-nav", "user" if i % 2 == 0 else "assistant", f"turn {i}", thread_id=thread, workspace="normal_chat") for i in range(40)]
    app.session_state["focus_normal_chat"] = {"thread_id": thread, "message_id": ids[10]}
    app.run()
    assert not app.exception
    assert any(f"around message #{ids[10]}" in c.value for c in app.caption)
    shown = [m.value for m in app.markdown if m.value.startswith("turn ")]
    assert "turn 10" in shown and "turn 39" not in shown
    next(b for b in app.button if b.label == "Back to latest").click().run()
    assert not app.exception
    shown = [m.value for m in app.markdown if m.value.startswith("turn ")]
    assert "turn 39" in shown and "focus_normal_chat" not in app.session_state


def test_finished_spatial_mission_renders_the_scene_preview(app):
    from orchestrator import spatial, vault
    app.query_params["scope"] = "visitor-scene"
    app.query_params["ws"] = "task_finder"
    app.run()
    thread = int(vault.active_thread("visitor-scene", "task_finder")["id"])
    placed = spatial.solve_layout(spatial.parse_scene({"room": [5, 4, 3], "objects": [{"name": "desk", "size": [1.6, 0.8, 0.75]}]}))
    artifact_id, _ = vault.save_artifact("visitor-scene", "scene-1.json", "missions/scene-1.json", spatial.scene_json(placed), "json")
    job_id = vault.enqueue_job("visitor-scene", "mission", {"goal": "room layout"}, thread_id=thread)
    vault.claim_job("t", ("mission",))
    vault.finish_job(job_id, "done", {"thread_id": thread, "goal": "room layout", "steps": 3, "succeeded": 3, "failed": 0, "truncated": 0, "failures": [], "scene_artifact": artifact_id, "scene_report": placed.report})
    app.run()
    assert not app.exception
    assert any("Layout resolved by the solver" in i.value for i in app.info)
    assert any(b.label.startswith("⬇ Download scene") for b in app.button) or "scene_preview" in app.session_state
    assert app.session_state["scene_preview"]["objects"][0]["name"] == "desk"


MISSION_ANSWER = """Here is a plan.

```mission
statement: Ship the landing page
nodes:
  - title: Draft copy
    executor: model
    task_type: chat
    instruction: Write the hero copy
  - title: Push it
    executor: connector
    config: {connector: github.push, owner_repo: me/proj, files: [{path: a.md, body: hi}]}
```
"""


def test_mission_block_in_chat_lands_in_task_finder_and_push_gates_launch(app, monkeypatch):
    from orchestrator import vault
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-the-launch-button")
    app.query_params["scope"] = "visitor-handoff"
    app.query_params["ws"] = "normal_chat"
    app.run()
    vault.append_message("visitor-handoff", "user", "plan this as a mission", workspace="normal_chat")
    vault.append_message("visitor-handoff", "assistant", MISSION_ANSWER, provider="fake/m", workspace="normal_chat")
    app.run()
    assert not app.exception
    send = next(b for b in app.button if b.label == "Send to Task Finder")
    send.click().run()
    assert not app.exception
    assert app.session_state["workspace_select"] == "task_finder"
    assert any("Mission from the chat" in m.value for m in app.markdown)
    thread = int(vault.active_thread("visitor-handoff", "task_finder")["id"])
    plan = app.session_state["pending_plans"][thread]["plan"]
    assert [n["executor"] for n in plan] == ["model", "connector"] and plan[1]["config"]["connector"] == "github.push"
    assert any("push slot armed" in e.value for e in app.error)
    launch = next(b for b in app.button if b.label.startswith("Launch workstreams"))
    assert launch.proto.disabled is True
    assert app.selectbox(key=f"node_{thread}_2_connector").value == "github.push"
    # Refine in chat posts the block into Normal Chat without a provider call.
    next(b for b in app.button if b.label == "Refine in chat").click().run()
    assert not app.exception
    assert app.session_state["workspace_select"] == "normal_chat"
    rows = vault.recent_messages("visitor-handoff", 10, workspace="normal_chat")
    assert rows[-1]["role"] == "user" and "```mission" in rows[-1]["content"] and "github.push" in rows[-1]["content"]


def test_rerun_reopens_stored_nodes_and_launch_stores_them(app, monkeypatch):
    from orchestrator import vault
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-the-launch-button")
    app.query_params["scope"] = "visitor-nodes"
    app.query_params["ws"] = "task_finder"
    app.run()
    app.chat_input[0].set_value("Research the history of tidal power").run()
    next(b for b in app.button if b.label.startswith("Launch workstreams")).click().run()
    assert not app.exception
    thread = int(vault.active_thread("visitor-nodes", "task_finder")["id"])
    stored = vault.mission_nodes_for(thread)
    assert stored and stored[0]["executor"] == "model" and stored[0]["on_failure"] == "stop"
    job = vault.list_jobs("visitor-nodes", kind="mission")[0]
    vault.claim_job("t", ("mission",))
    vault.finish_job(int(job["id"]), "done", {"thread_id": thread, "goal": "x", "steps": 1, "succeeded": 1, "failed": 0, "truncated": 0, "failures": [], "stopped_at": ""})
    vault.set_thread_mission(thread, "Research the history of tidal power")
    app.run()
    assert not app.exception
    next(b for b in app.button if b.label == "Re-run this mission").click().run()
    assert not app.exception
    assert any("Mission from the chat" in m.value for m in app.markdown)
    assert app.session_state["pending_plans"][thread]["plan"][0]["title"] == stored[0]["title"]


def test_canvas_shows_the_chats_last_mockup_after_a_reload_and_explains_a_pasted_link(app):
    from orchestrator import vault
    app.query_params["scope"] = "visitor-canvas"
    app.run()
    thread = int(vault.active_thread("visitor-canvas", "normal_chat")["id"])
    vault.append_message("visitor-canvas", "user", "make me a dashboard", thread_id=thread, workspace="normal_chat")
    vault.append_message("visitor-canvas", "assistant", "Sure:\n```html\n<main><h1>Dash</h1><button>Go</button>\n```\nOpen it at https://example.com/dash", thread_id=thread, workspace="normal_chat")
    app.run()  # a fresh session (a reload after a crashed run) still finds the mockup in the chat
    assert not app.exception
    assert app.session_state["preview_source"] == "<main><h1>Dash</h1><button>Go</button>"
    editor = next(t for t in app.text_area if t.key == "preview_editor")
    assert editor.value == "<main><h1>Dash</h1><button>Go</button>"
    editor.set_value("https://example.com/dash").run()
    next(b for b in app.button if b.key == "render_preview").click().run()
    assert not app.exception
    assert any("never fetches pages" in i.value for i in app.info)
    editor = next(t for t in app.text_area if t.key == "preview_editor")
    editor.set_value("").run()
    next(b for b in app.button if b.key == "render_preview").click().run()
    assert not app.exception and app.session_state["preview_source"] == "" and app.session_state["preview_cleared"] is True
    app.run()
    assert app.session_state["preview_source"] == ""  # an emptied canvas is not refilled from the chat behind the operator's back
