"""Batch U2: the operator is told the truth.

Run mode selects itself for a page with scripts; the canvas carries a status line; a cut answer gets a box with
Continue / Raise the limit / Smaller page; a failed send is a system row in the thread with its route row; every
answer can say why it came out as it did; the health view reports the process; browser errors are said plainly.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import proctor, router, sandbox_preview as sp, vault  # noqa: E402
from orchestrator.router import ProviderError, RouteDecision  # noqa: E402
from tests.test_batch_t_ui import PAGE1, captions, fake_provider, fenced, rows, seed_page  # noqa: E402
from tests.test_batch_t_ui import app as app_fixture  # noqa: E402

app = app_fixture  # the AppTest fixture (tmp vault, no keys, no workers), under its own name so the tests' parameter reads plainly


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------

def test_browser_errors_are_said_plainly():
    assert "cut off" in sp.plain_script_error("Uncaught SyntaxError: Unexpected end of input")
    assert "cut off" in sp.plain_script_error("SyntaxError: Unterminated template literal")
    assert sp.plain_script_error("Uncaught ReferenceError: foo is not defined") == "the page calls foo, which it never defined"
    assert "not there" in sp.plain_script_error("TypeError: Cannot read properties of null (reading 'addEventListener')")
    assert "inline" in sp.plain_script_error("Failed to resolve module specifier 'three'")
    assert sp.plain_script_error("Something else entirely") == "Something else entirely"


def test_same_error_reason_names_the_cut():
    error = {"seq": 2, "status": "error", "errors": [{"message": "x", "line": 1}], "blocked": []}
    same = {"rounds": 1, "last_signature": sp.error_signature(error), "handled_seq": 0}
    allowed, reason, settled = sp.fix_decision(same, error, True, False)
    assert not allowed and settled and reason.startswith("repair stopped: the rewrite broke in the same place")


def test_route_facts_and_export_carry_the_why(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    scope = "u2-routes"
    user = vault.append_message(scope, "user", "make a page", workspace="normal_chat")
    answer = vault.append_message(scope, "assistant", "here", provider="groq/x", workspace="normal_chat", finish="length")
    vault.record_route(scope, "normal_chat", "chat", "groq/x", "normal", 3200, "length", "cheapest fit", runner_up="gemini/y", message_id=answer)
    facts = vault.routes_for_messages(scope, [user, answer])
    assert set(facts) == {answer}
    sentence = vault.route_facts(facts[answer])
    assert sentence.startswith("Sent to groq/x in normal mode because cheapest fit.")
    assert "runner-up was gemini/y" in sentence and "3.2 s" in sentence and "cut at the answer length limit" in sentence
    thread_id = int(vault.active_thread(scope, "normal_chat")["id"])
    payload = vault.export_thread(thread_id, scope)
    assert [route["message_id"] for route in payload["routes"]] == [answer]
    _, markdown = vault.thread_transcript(thread_id, "markdown", scope)
    assert "_Why: Sent to groq/x" in markdown
    health = vault.health_check()
    assert health["ok"] and health["jobs"] == {} and health["sends"] == 1 and health["last_send_at"] > 0


# ----------------------------------------------------------------------------
# The app
# ----------------------------------------------------------------------------

def send(app, text: str):
    app.chat_input[0].set_value(text).run()
    assert not app.exception


def test_a_page_with_scripts_switches_the_canvas_to_run_mode(app, monkeypatch):
    fake_provider(monkeypatch, [fenced(PAGE1)])
    app.query_params["scope"] = "u2-run"
    app.query_params["ws"] = "normal_chat"
    app.run()
    assert app.radio(key="preview_mode").value == "Preview only (buttons off)"
    send(app, "make me a page with a button")
    assert app.session_state["preview_source"] == PAGE1
    assert app.radio(key="preview_mode").value == "Run the page"
    assert any(c.startswith("Run mode was switched on because this page has scripts.") for c in captions(app)), captions(app)
    status = next(c for c in captions(app) if c.startswith("Run the page · page "))
    assert "repairs used 0 of 2" in status and "answer limit" in status
    stored = json.loads(vault.setting_get("u2-run", "preview_state"))
    assert stored["mode"] == "Run the page"


def test_a_mode_name_from_an_earlier_build_is_restored(app):
    vault.initialize_database()
    vault.setting_set("u2-legacy", "preview_state", json.dumps({"source": PAGE1, "mode": "Run in sandbox", "complete": True, "reasons": []}))
    app.query_params["scope"] = "u2-legacy"
    app.run()
    assert not app.exception
    assert app.radio(key="preview_mode").value == "Run the page" and app.session_state["preview_source"] == PAGE1


def test_a_failed_send_is_a_system_row_with_its_route(app, monkeypatch):
    fake_provider(monkeypatch, [ProviderError("groq: 503 service unavailable")])
    app.query_params["scope"] = "u2-failed"
    app.query_params["ws"] = "normal_chat"
    app.run()
    send(app, "hello")
    stored = rows("u2-failed")
    assert [row["role"] for row in stored] == ["user", "system"]
    assert stored[-1]["content"].startswith("The send failed:") and stored[-1]["finish"] == "failed"
    route = vault.recent_routes("u2-failed", 1)[0]
    assert route["route"] == "failed" and int(route["message_id"]) == int(stored[-1]["id"])
    app.run()  # the thread shows the failure where the question is
    assert any(i.value.startswith("The send failed:") for i in app.info), [i.value for i in app.info]


def cut_provider(monkeypatch):
    """Answers that the vendor cut at the output budget (prose, so the app has nothing to continue on its own)."""
    calls = []

    def answer(task_type, messages, *args, **kwargs):
        calls.append(messages)
        return "Half an answer that stops", RouteDecision("fake", "m", task_type, "r", finish="length")

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-the-cut-box")
    monkeypatch.setattr(router, "heavy_stream", answer)
    monkeypatch.setattr(router, "generate_mode", lambda mode, task_type, messages, *a, **k: answer(task_type, messages))
    monkeypatch.setattr(router, "cortex_wait_seconds", lambda *a, **k: 0.0)
    monkeypatch.setattr(proctor, "cached_fragility", lambda *a, **k: None)
    return calls


def test_a_cut_answer_gets_the_box_and_continue_sends(app, monkeypatch):
    calls = cut_provider(monkeypatch)
    app.query_params["scope"] = "u2-cut"
    app.query_params["ws"] = "normal_chat"
    app.run()
    send(app, "write me a long story")
    answer_id = int(rows("u2-cut")[-1]["id"])
    assert any("cut off at the answer length limit (4,096)" in w.value for w in app.warning), [w.value for w in app.warning]
    app.run()  # the history shows the same box on the last cut answer
    assert any(b.key == f"continue_{answer_id}" for b in app.button)
    app.button(key=f"continue_{answer_id}").click().run()
    assert not app.exception and len(calls) == 2
    assert calls[1][-1]["role"] == "user" and calls[1][-1]["content"].startswith("Continue your previous answer")
    assert [row["role"] for row in rows("u2-cut")] == ["user", "assistant", "user", "assistant"]
    expanders = [e.label for e in app.expander]
    assert "Why this answer came out like this" in expanders


def test_raise_the_limit_doubles_the_slider_and_continues(app, monkeypatch):
    calls = cut_provider(monkeypatch)
    app.query_params["scope"] = "u2-raise"
    app.query_params["ws"] = "chat_bot"
    app.run()
    assert app.slider(key="output_token_budget").value == 4096
    send(app, "write me a long story")
    answer_id = int(rows("u2-raise", "chat_bot")[-1]["id"])
    app.run()
    app.button(key=f"raise_{answer_id}").click().run()
    assert not app.exception and len(calls) == 2
    assert app.slider(key="output_token_budget").value == 8192 and app.session_state["max_tokens"] == 8192
    app.button(key=f"smaller_{int(rows('u2-raise', 'chat_bot')[-1]['id'])}").click().run()
    assert len(calls) == 3 and calls[2][-1]["content"].startswith("Rewrite the page so it fits")


def test_keys_pasted_in_an_earlier_session_are_named(app):
    vault.initialize_database()
    vault.setting_set("u2-keys", "keys_seen", "1700000000")
    app.query_params["scope"] = "u2-keys"
    app.run()
    assert not app.exception
    assert any(i.value.startswith("Keys were pasted in an earlier session.") for i in app.info), [i.value for i in app.info]
    assert vault.setting_get("u2-keys", "last_seen") != ""


def test_health_view_reports_the_process(app):
    app.query_params["health"] = "1"
    app.run()
    payload = json.loads(app.code[0].value)
    assert payload["status"] == "ok" and payload["process"]["workers"] == 0 and payload["process"]["age_s"] >= 0
    assert payload["vault"]["jobs"] == {} and "age_s" in payload["snapshots"] and isinstance(payload["ledger"], dict)


def test_captions_are_plain(app):
    seed_page(app, "u2-plain", PAGE1)
    assert any(c.startswith("Buttons and other interactive parts are switched off") for c in captions(app))
    labels = [c.label for c in app.sidebar.checkbox]
    assert "Summarise long chats automatically" in labels and "Auto-migrate heavy threads" not in labels
    assert not any("Thread health agent" in s.value for s in app.sidebar.subheader)
