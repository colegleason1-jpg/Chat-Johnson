"""Batch T (agent C): Run mode on the canvas, the automatic fix loop, preview buttons, and per-input Clear buttons.

AppTest runs no JavaScript, so the sandbox component never reports on its own; the app's ``sandbox_report_injected``
seam stands in for the frame's talk-back (it is consumed once per run, so a re-delivered report is injected again with
the same seq). Providers are faked at the router boundary the way the batch P tests do.
"""
import json
import os
import sys
from urllib.parse import quote

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import proctor, quota_registry, router, vault  # noqa: E402
from orchestrator.router import RouteDecision  # noqa: E402
from orchestrator.sandbox_preview import FIX_MARKER, FIX_PREFIX, page_hash  # noqa: E402

KEY_ENVS = ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY")
PAGE1 = "<!doctype html><html><head><title>One</title></head><body><h1>Page one</h1><script>foo();</script></body></html>"
PAGE2 = "<!doctype html><html><head><title>Two</title></head><body><h1>Page two</h1><script>bar();</script></body></html>"
PAGE3 = "<!doctype html><html><head><title>Three</title></head><body><h1>Page three</h1><script>baz();</script></body></html>"
ERROR_FOO = {"message": "Uncaught ReferenceError: foo is not defined", "line": 9, "column": 1, "stack": ""}
ERROR_BAR = {"message": "Uncaught ReferenceError: bar is not defined", "line": 3, "column": 5, "stack": ""}
ERROR_BAZ = {"message": "Uncaught ReferenceError: baz is not defined", "line": 4, "column": 2, "stack": ""}
SETTING = "sandbox_fix"  # the one vault setting per scope that mirrors rounds and signatures
# Keys of text widgets that live inside st.form blocks (a form forbids extra buttons).
FORM_KEY_PREFIXES = ("byok_", "model_override_")
SWITCH_CAPTION = "Automatic fixes run from Normal Chat or Chat Bot; switch there and this page is fixed."


def fenced(page: str) -> str:
    return f"Here you go:\n```html\n{page}\n```"


def report(page: str, error: dict, seq: int = 1) -> dict:
    return {"seq": seq, "page": page_hash(page), "status": "error", "errors": [error], "blocked": [], "console": []}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.setenv("CHAT_JOHNSON_BUILD", "abc1234")
    monkeypatch.setenv("CHAT_JOHNSON_JOB_WORKERS", "0")
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delattr(st, "segmented_control", raising=False)
    quota_registry.reset_for_tests()
    yield AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
    quota_registry.reset_for_tests()


def fake_provider(monkeypatch, answers):
    """Every route returns the next canned answer (an exception instance is raised instead); the prompt messages of each
    call are recorded."""
    calls = []
    queue = list(answers)

    def answer(task_type, messages, *args, **kwargs):
        calls.append(messages)
        text = queue.pop(0) if queue else "no page"
        if isinstance(text, BaseException):
            raise text
        return text, RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-the-fix-loop")
    monkeypatch.setattr(router, "heavy_stream", answer)
    monkeypatch.setattr(router, "generate_mode", lambda mode, task_type, messages, *a, **k: answer(task_type, messages))
    monkeypatch.setattr(router, "cortex_wait_seconds", lambda *a, **k: 0.0)
    monkeypatch.setattr(proctor, "cached_fragility", lambda *a, **k: None)
    return calls


def seed_page(app, scope: str, page: str, workspace: str = "normal_chat"):
    app.query_params["scope"] = scope
    app.query_params["ws"] = workspace
    app.run()
    assert not app.exception
    vault.append_message(scope, "user", "make me a page", workspace=workspace)
    vault.append_message(scope, "assistant", fenced(page), provider="fake/m", workspace=workspace)
    app.run()
    assert not app.exception
    assert app.session_state["preview_source"] == page


def run_mode(app, heavy: bool):
    if heavy:
        app.checkbox(key="heavy_mode").check().run()
    app.radio(key="preview_mode").set_value("Run the page").run()
    assert not app.exception


def inject(app, page: str, error: dict, seq: int = 1):
    app.session_state["sandbox_report_injected"] = report(page, error, seq)
    app.run()
    assert not app.exception


def inject_raw(app, raw: dict):
    app.session_state["sandbox_report_injected"] = raw
    app.run()
    assert not app.exception


def captions(app):
    return [c.value for c in app.caption]


def rows(scope: str, workspace: str = "normal_chat"):
    return vault.recent_messages(scope, 20, workspace=workspace)


def stored(scope: str, page: str) -> dict:
    table = json.loads(vault.setting_get(scope, SETTING) or "{}")
    return table.get(page_hash(page)) or {}


def iframe_srcdocs(app):
    docs = []
    for element in app.get("iframe"):
        proto = getattr(element.proto, "iframe", element.proto)
        docs.append(str(getattr(proto, "srcdoc", "")))
    return docs


def test_clear_buttons_empty_one_field_only(app):
    app.query_params["scope"] = "visitor-clear"
    app.run()
    assert app.text_area(key="preview_editor").proto.placeholder.startswith("Paste a page here, or ask Normal Chat or Chat Bot")
    app.text_input(key="nav_query_normal_chat").input("hello").run()
    app.text_area(key="preview_editor").input("<p>keep me</p>").run()
    app.button(key="render_preview").click().run()
    assert not app.exception and app.session_state["preview_source"] == "<p>keep me</p>"
    app.button(key="clear__nav_query_normal_chat").click().run()
    assert not app.exception
    assert app.text_input(key="nav_query_normal_chat").value == ""
    assert app.text_area(key="preview_editor").value == "<p>keep me</p>" and app.session_state["preview_source"] == "<p>keep me</p>"
    app.text_input(key="nav_query_normal_chat").input("hello again").run()
    app.button(key="clear__preview_editor").click().run()
    assert not app.exception
    assert app.text_area(key="preview_editor").value == "" and app.session_state["preview_source"] == ""
    assert app.session_state["preview_cleared"] is True
    assert app.text_input(key="nav_query_normal_chat").value == "hello again"
    # A keyed widget that also passes value= (the paid slot model) clears without raising.
    assert app.text_input(key="paid_slot_model").value == "o3-mini"
    app.button(key="clear__paid_slot_model").click().run()
    assert not app.exception and app.text_input(key="paid_slot_model").value == ""


@pytest.mark.parametrize("workspace", ["normal_chat", "chat_bot", "repository"])
def test_every_text_widget_outside_a_form_has_its_own_clear_button(app, workspace):
    app.query_params["scope"] = "visitor-buttons"
    app.query_params["ws"] = workspace
    app.run()
    assert not app.exception
    widgets = list(app.text_input) + list(app.text_area)
    buttons = {b.key for b in app.button}
    checked = []
    for widget in widgets:
        key = widget.key
        if key is None or key.startswith(FORM_KEY_PREFIXES):
            # Only a form may hold a widget without its own Clear button (Streamlit forbids extra buttons there).
            assert widget.proto.form_id, f"{key or widget.label!r} is outside a form and has no Clear button"
            continue
        assert f"clear__{key}" in buttons, f"{key} has no Clear button"
        checked.append(key)
    expected = {"preview_editor", f"nav_query_{workspace}", "paid_slot_key", "paid_slot_model", "github_push_token", "scope_switch_input", "artifact_query"}
    assert expected <= set(checked), sorted(expected - set(checked))
    if workspace == "repository":
        assert {"repo_manual", "repo_goal", "repo_fetch_ref"} <= set(checked)


def test_download_button_and_data_link_decoding(app):
    seed_page(app, "visitor-download", PAGE1)
    download = next(d for d in app.download_button if d.key == "preview_download")
    assert download.label == "Download preview (.html)" and download.proto.disabled is False
    assert any(c.startswith("Buttons and other interactive parts are switched off") for c in captions(app))
    assert any(c.startswith("This page has scripts.") for c in captions(app))  # PAGE1 carries a script
    link = "data:text/html," + quote("<section><h2>Decoded mockup</h2></section>", safe="")
    app.text_area(key="preview_editor").set_value(link).run()
    app.button(key="render_preview").click().run()
    assert not app.exception
    assert not any("never fetches pages" in i.value for i in app.info)
    assert any("Decoded mockup" in doc for doc in iframe_srcdocs(app))
    assert not any(c.startswith("This page has scripts.") for c in captions(app))


def test_run_mode_executes_only_what_render_committed(app):
    seed_page(app, "visitor-draft", PAGE1)
    run_mode(app, heavy=False)
    assert any(c.endswith("automatic fixes run in Normal Chat or Chat Bot with Heavy Mode on.") for c in captions(app))
    assert not any(c == "Press Render to run what is in the box." for c in captions(app))
    app.text_area(key="preview_editor").set_value(PAGE2).run()
    assert not app.exception
    assert app.session_state["preview_source"] == PAGE1  # the draft is not run until Render commits it
    assert any(c == "Press Render to run what is in the box." for c in captions(app))
    app.button(key="render_preview").click().run()
    assert app.session_state["preview_source"] == PAGE2
    assert not any(c == "Press Render to run what is in the box." for c in captions(app))


def test_hazards_are_named_before_the_page_runs(app):
    app.query_params["scope"] = "visitor-hazard"
    app.run()
    app.text_area(key="preview_editor").set_value('<html><head><base href="https://x.test/"><meta http-equiv="refresh" content="1"></head><body>hi</body></html>').run()
    app.button(key="render_preview").click().run()
    run_mode(app, heavy=False)
    assert any(c.startswith("Before running, the sandbox removed ") and "a base tag" in c and "a meta refresh" in c and c.endswith(".") for c in captions(app)), captions(app)


def test_run_mode_without_heavy_reports_but_never_fixes(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-run-normal", PAGE1)
    run_mode(app, heavy=False)
    inject(app, PAGE1, ERROR_FOO)
    assert any("Heavy Mode is off" in c for c in captions(app)), captions(app)
    assert any("1 error(s)" in c for c in captions(app))
    assert any("foo is not defined" in t.value for t in app.text)
    assert len(rows("visitor-run-normal")) == 2 and calls == []
    assert "sandbox_fix_pending" not in app.session_state
    assert vault.setting_get("visitor-run-normal", SETTING) == ""


def test_a_heavy_off_report_stays_open_until_heavy_is_ticked(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-open", PAGE1)
    run_mode(app, heavy=False)
    inject(app, PAGE1, ERROR_FOO)
    assert calls == [] and vault.setting_get("visitor-open", SETTING) == ""
    assert app.session_state["sandbox_fix"][page_hash(PAGE1)]["handled_seq"] == 0
    app.checkbox(key="heavy_mode").check().run()
    assert not app.exception and calls == []
    inject(app, PAGE1, ERROR_FOO)  # the same report, re-delivered with the same seq
    assert len(calls) == 1 and app.session_state["preview_source"] == PAGE2
    assert stored("visitor-open", PAGE1)["rounds"] == 1


def test_heavy_run_mode_sends_one_fix_turn(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-fix", PAGE1)
    old = {f"old{i:02d}": {"rounds": 1, "last_signature": "s"} for i in range(8)}
    vault.setting_set("visitor-fix", SETTING, json.dumps(old))
    run_mode(app, heavy=True)
    assert any(c.endswith("and are fixed automatically, up to 2 rounds.") for c in captions(app))
    inject(app, PAGE1, ERROR_FOO)
    history = rows("visitor-fix")
    assert len(history) == 4 and len(calls) == 1
    assert history[-2]["role"] == "user" and history[-2]["content"].startswith(f"{FIX_PREFIX}1/2")
    assert "foo is not defined" in history[-2]["content"] and PAGE1 in history[-2]["content"]
    assert history[-1]["role"] == "assistant" and history[-1]["content"] == fenced(PAGE2)
    assert app.session_state["preview_source"] == PAGE2 and app.session_state["preview_editor"] == PAGE2
    system = "\n".join(m["content"] for m in calls[0] if m["role"] == "system")
    assert "PREVIEW RULES" in system and "automatic repair turn" in system
    assert stored("visitor-fix", PAGE1)["rounds"] == 1 and stored("visitor-fix", PAGE2)["rounds"] == 1  # lineage
    table = json.loads(vault.setting_get("visitor-fix", SETTING))
    assert len(table) == 8 and "old00" not in table and "old01" not in table and "old02" in table  # capped, oldest first
    assert any("round 1 of 2" in c for c in captions(app))
    assert any(c.endswith("· automatic fix") for c in captions(app))  # the assistant bubble of the fix turn
    assert not any(FIX_MARKER in t.value or PAGE1 in t.value for t in app.text)  # the bubble shows the report only
    assert any(t.value.startswith(f"{FIX_PREFIX}1/2") and "foo is not defined" in t.value for t in app.text)


def two_rounds(app, monkeypatch, scope: str):
    calls = fake_provider(monkeypatch, [fenced(PAGE2), fenced(PAGE3)])
    seed_page(app, scope, PAGE1)
    run_mode(app, heavy=True)
    inject(app, PAGE1, ERROR_FOO)
    assert app.session_state["preview_source"] == PAGE2 and len(calls) == 1
    inject(app, PAGE2, ERROR_BAR)
    assert app.session_state["preview_source"] == PAGE3 and len(calls) == 2
    return calls


def test_two_rounds_are_the_cap(app, monkeypatch):
    calls = two_rounds(app, monkeypatch, "visitor-cap")
    inject(app, PAGE3, ERROR_BAZ)
    assert len(calls) == 2 and len(rows("visitor-cap")) == 6
    assert any("2 automatic rounds used" in c for c in captions(app)), captions(app)
    assert stored("visitor-cap", PAGE3)["rounds"] == 2


def test_the_same_error_coming_back_stops_the_loop(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2), fenced(PAGE3)])
    seed_page(app, "visitor-same", PAGE1)
    run_mode(app, heavy=True)
    inject(app, PAGE1, ERROR_FOO)
    assert app.session_state["preview_source"] == PAGE2 and len(calls) == 1
    inject(app, PAGE2, ERROR_FOO)
    assert len(calls) == 1 and app.session_state["preview_source"] == PAGE2
    assert any("broke in the same place" in c for c in captions(app)), captions(app)


def test_a_fix_that_returns_the_same_page_runs_it_again(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE1), fenced(PAGE2)])
    seed_page(app, "visitor-again", PAGE1)
    run_mode(app, heavy=True)
    assert app.session_state["sandbox_seq"][page_hash(PAGE1)] == 1
    inject(app, PAGE1, ERROR_FOO)
    assert len(calls) == 1 and app.session_state["preview_source"] == PAGE1
    assert app.session_state["sandbox_seq"][page_hash(PAGE1)] == 2
    assert any(c == "The fix returned the same page; running it again." for c in captions(app)), captions(app)
    assert stored("visitor-again", PAGE1)["rounds"] == 1
    inject(app, PAGE1, ERROR_FOO, seq=2)  # the re-run reports the same error: the loop stops
    assert len(calls) == 1 and any("broke in the same place" in c for c in captions(app))
    inject(app, PAGE1, ERROR_BAR, seq=3)  # a different error on the same page earns the second round
    assert len(calls) == 2 and app.session_state["preview_source"] == PAGE2


def test_a_failed_fix_turn_costs_no_round(app, monkeypatch):
    calls = fake_provider(monkeypatch, [RuntimeError("provider down"), fenced(PAGE2)])
    seed_page(app, "visitor-failed", PAGE1)
    run_mode(app, heavy=True)
    inject(app, PAGE1, ERROR_FOO)
    assert len(calls) == 1 and app.session_state["preview_source"] == PAGE1
    assert any(c.startswith("Automatic fix did not complete") and c.endswith("press Render to try again.") for c in captions(app)), captions(app)
    assert vault.setting_get("visitor-failed", SETTING) == ""
    app.button(key="render_preview").click().run()  # Render runs the page again (seq 2) and the new report is fixed
    inject(app, PAGE1, ERROR_FOO, seq=2)
    assert len(calls) == 2 and app.session_state["preview_source"] == PAGE2
    assert stored("visitor-failed", PAGE1)["rounds"] == 1


def test_a_report_in_another_workspace_waits_for_a_fixing_one(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    app.query_params["scope"] = "visitor-elsewhere"
    app.query_params["ws"] = "repository"
    app.run()
    assert not app.exception
    app.text_area(key="preview_editor").set_value(PAGE1).run()
    app.button(key="render_preview").click().run()
    run_mode(app, heavy=True)
    assert any(c.endswith("automatic fixes run in Normal Chat or Chat Bot with Heavy Mode on.") for c in captions(app))
    inject(app, PAGE1, ERROR_FOO)
    assert calls == [] and "sandbox_fix_pending" not in app.session_state
    assert any(c == SWITCH_CAPTION for c in captions(app)), captions(app)
    assert app.session_state["sandbox_fix"][page_hash(PAGE1)]["handled_seq"] == 0  # left open for the switch
    app.radio(key="workspace_select").set_value("normal_chat").run()
    assert not app.exception and calls == []
    inject(app, PAGE1, ERROR_FOO)  # the same seq, decided again where a fix can run
    assert len(calls) == 1 and app.session_state["preview_source"] == PAGE2
    assert stored("visitor-elsewhere", PAGE1)["rounds"] == 1


def test_a_running_generation_blocks_a_fix(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-lock", PAGE1)
    run_mode(app, heavy=True)
    lock = quota_registry.get_request_lock()
    assert lock.acquire(timeout=1)
    try:
        inject(app, PAGE1, ERROR_FOO)
    finally:
        lock.release()
    assert calls == [] and len(rows("visitor-lock")) == 2
    assert any("a generation is already running" in c for c in captions(app)), captions(app)
    assert app.session_state["sandbox_fix"][page_hash(PAGE1)]["handled_seq"] == 0


def test_a_clean_page_with_blocked_assets_is_not_fixed(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-assets", PAGE1)
    run_mode(app, heavy=True)
    inject_raw(app, {"seq": 1, "page": page_hash(PAGE1), "status": "ready", "errors": [], "blocked": ["img-src https://cdn.example/a.png"], "console": []})
    assert calls == [] and "sandbox_fix_pending" not in app.session_state
    assert any(c == "Blocked images, fonts or connections are not fixed automatically; ask the chat to inline them." for c in captions(app)), captions(app)
    assert any("1 blocked" in c for c in captions(app))
    assert app.session_state["sandbox_fix"][page_hash(PAGE1)]["handled_seq"] == 1


def test_a_blocked_status_is_fixed(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-blocked", PAGE1)
    run_mode(app, heavy=True)
    inject_raw(app, {"seq": 1, "page": page_hash(PAGE1), "status": "blocked", "errors": [], "blocked": ["script-src-elem https://cdn.example/x.js"], "console": []})
    assert len(calls) == 1 and app.session_state["preview_source"] == PAGE2
    turn = rows("visitor-blocked")[-2]["content"]
    assert turn.startswith(f"{FIX_PREFIX}1/2") and "status: blocked" in turn and "cdn.example/x.js" in turn


def test_a_chat_message_drops_the_pending_fix(app, monkeypatch):
    calls = fake_provider(monkeypatch, ["Hello back."])
    seed_page(app, "visitor-first", PAGE1)
    app.session_state["sandbox_fix_pending"] = {
        "workspace": "normal_chat", "page": page_hash(PAGE1), "prompt": f"{FIX_PREFIX}1/2: stale", "round": 1, "signature": "sig",
    }
    app.chat_input[0].set_value("hello there").run()
    assert not app.exception
    assert any(c == "Automatic fix dropped: your message goes first." for c in captions(app)), captions(app)
    assert "sandbox_fix_pending" not in app.session_state
    history = rows("visitor-first")
    assert len(calls) == 1 and len(history) == 4 and history[-2]["content"] == "hello there"
    assert vault.setting_get("visitor-first", SETTING) == ""


def test_a_fix_for_a_page_no_longer_on_the_canvas_is_dropped(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-moved", PAGE1)
    app.checkbox(key="heavy_mode").check().run()
    app.session_state["sandbox_fix_pending"] = {
        "workspace": "normal_chat", "page": page_hash(PAGE3), "prompt": f"{FIX_PREFIX}1/2: stale", "round": 1, "signature": "sig",
    }
    app.run()
    assert not app.exception and calls == []
    assert any(c == "Automatic fix dropped: the canvas moved on." for c in captions(app)), captions(app)
    assert "sandbox_fix_pending" not in app.session_state and vault.setting_get("visitor-moved", SETTING) == ""


def test_sanitized_mode_ignores_reports(app, monkeypatch):
    calls = fake_provider(monkeypatch, [fenced(PAGE2)])
    seed_page(app, "visitor-sanitized", PAGE1)
    app.checkbox(key="heavy_mode").check().run()
    assert app.radio(key="preview_mode").value == "Preview only (buttons off)"
    inject(app, PAGE1, ERROR_FOO)
    assert calls == [] and len(rows("visitor-sanitized")) == 2
    assert app.session_state["preview_source"] == PAGE1 and "sandbox_fix_pending" not in app.session_state
    assert not any("Heavy Mode is off" in c or "automatic fix round" in c or c.startswith("Sandbox:") for c in captions(app))
    assert vault.setting_get("visitor-sanitized", SETTING) == ""


def test_the_vault_mirror_survives_a_fresh_session(app, monkeypatch):
    two_rounds(app, monkeypatch, "visitor-mirror")
    fresh = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
    calls = fake_provider(monkeypatch, [fenced("<p>never</p>")])
    fresh.query_params["scope"] = "visitor-mirror"
    fresh.query_params["ws"] = "normal_chat"
    fresh.run()
    assert not fresh.exception and fresh.session_state["preview_source"] == PAGE3  # refilled from the chat
    run_mode(fresh, heavy=True)
    inject(fresh, PAGE3, ERROR_BAZ)
    assert calls == [] and len(rows("visitor-mirror")) == 6
    assert any("2 automatic rounds used" in c for c in captions(fresh)), captions(fresh)


def test_a_stored_fix_turn_is_shown_as_text_never_markdown(app):
    app.query_params["scope"] = "visitor-history"
    app.query_params["ws"] = "normal_chat"
    app.run()
    turn = (
        f"{FIX_PREFIX}1/2: the page you generated was run in the preview sandbox and did not work.\n\n"
        "Sandbox report (status: error)\nErrors (1):\n- line 9: foo is not defined\nBlocked (0): none\nConsole (0 of the last lines):\n- (empty)\n\n"
        f"{FIX_MARKER}\nFix it.\n\n```html\n<img src=x>![](https://evil.test/p.png)\n```"
    )
    vault.append_message("visitor-history", "user", turn, workspace="normal_chat")
    vault.append_message("visitor-history", "assistant", "Done.", provider="fake/m", workspace="normal_chat")
    app.run()
    assert not app.exception
    assert not any("evil.test" in m.value or "<img" in m.value for m in app.markdown)
    assert any(t.value.startswith(f"{FIX_PREFIX}1/2") and "foo is not defined" in t.value and "evil.test" not in t.value for t in app.text)
    assert any(c == "Automatic fix · from the sandbox report" for c in captions(app))
    assert any(e.label == "Sandbox report" for e in app.expander)
