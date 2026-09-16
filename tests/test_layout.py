"""The controls stay reachable.

The chat bar pins the page to its bottom, so anything rendered above a long conversation scrolls off the top of the
screen and cannot be tapped on a tablet. Two rules keep the app usable: the chat's own controls render below the
conversation (next to the chat bar), and the preview canvas only opens itself for a page produced in this session,
never for one restored from the vault on load (an open canvas adds a ~500px frame under the conversation).
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import vault  # noqa: E402
from tests.test_batch_t_ui import PAGE1, app as app_fixture, captions, fake_provider, fenced  # noqa: E402

app = app_fixture


def restore_page(scope: str, page: str = PAGE1) -> None:
    vault.initialize_database()
    vault.setting_set(scope, "preview_state", json.dumps({"source": page, "mode": "Run the page", "complete": True, "reasons": []}))


def test_the_canvas_stays_closed_for_a_restored_page_and_offers_a_button(app):
    restore_page("layout-restore")
    app.query_params["scope"] = "layout-restore"
    app.query_params["ws"] = "normal_chat"
    app.run()
    assert not app.exception
    assert app.session_state["preview_source"] == PAGE1  # the page is there
    assert "canvas_open" not in app.session_state  # but the canvas is not opened by the reload
    toggle = [b for b in app.button if b.key == "toggle_canvas"]
    assert toggle and toggle[0].label == "Show the page on the preview canvas"
    toggle[0].click().run()
    assert app.session_state["canvas_open"] is True
    assert next(b for b in app.button if b.key == "toggle_canvas").label == "Hide the preview canvas"


def test_a_page_produced_in_this_session_opens_the_canvas(app, monkeypatch):
    fake_provider(monkeypatch, [fenced(PAGE1)])
    app.query_params["scope"] = "layout-fresh"
    app.query_params["ws"] = "normal_chat"
    app.run()
    assert "canvas_open" not in app.session_state
    app.chat_input[0].set_value("make me a page with a button").run()
    assert not app.exception
    assert app.session_state["preview_source"] == PAGE1 and app.session_state["canvas_open"] is True


def test_the_chat_controls_render_below_the_conversation(app):
    """The four buttons must come after the history, or the bottom-pinned page puts them off the top of the screen."""
    source = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    for workspace, heading in (("normal_chat", "Conversation"), ("chat_bot", "Developer conversation")):
        body = source.split(f"def render_{workspace}(")[1].split("\ndef ")[0]
        history = body.index(f'render_history(project_scope, "{heading}"')
        controls = body.index(f'render_thread_bar(project_scope, "{workspace}"')
        assert history < controls, f"{workspace}: the thread controls must render after the conversation"
    restore_page("layout-order")
    app.query_params["scope"] = "layout-order"
    app.query_params["ws"] = "normal_chat"
    app.run()
    assert not app.exception
    keys = [b.key for b in app.button]
    for key in ("new_thread_normal_chat", "clear_thread_normal_chat", "delete_thread_normal_chat", "toggle_canvas"):
        assert key in keys, keys
    assert any("A single-pass terminal" in c for c in captions(app))
