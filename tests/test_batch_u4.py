"""Batch U4: missions that can fail out loud and see the page; patch mode for large pages.

A failed step posts a system row in the thread; a full free-tier window is a pause, not a failed step; the canvas
page travels into a mission that refers to it and back; ``preview.validate`` and ``preview.repair`` check and mend a
page statically; Task Finder says missions cannot run the browser and offers Chat Bot; a page over 6,000 characters
is edited with SEARCH/REPLACE blocks applied in Python.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import jobs, mission_runner, missions, pagepatch, proctor, router, vault  # noqa: E402
from orchestrator.prompting import build_prompt_messages  # noqa: E402
from orchestrator.router import ProviderError, RouteDecision  # noqa: E402
from tests.test_batch_t_ui import PAGE1, app as app_fixture, captions, fenced  # noqa: E402

app = app_fixture

BIG_PAGE = "<!doctype html><html><head><title>Big</title><style>\n" + "\n".join(f".row{i} {{ color: #{i:03d}; padding: 4px; }}" for i in range(300)) + "\n</style></head><body>\n<h1>Big page</h1>\n<button id=\"go\">Go</button>\n<script>document.getElementById('go').onclick = () => alert('hi');</script>\n</body></html>"
BROKEN_PAGE = "<!doctype html><html><head><script src=\"https://cdn.tailwindcss.com\"></script></head><body><h1>Half</h1><script>function go() {"
GOOD_PAGE = "<!doctype html><html><head><title>Fixed</title><style>h1{color:teal}</style></head><body><h1>Fixed</h1><script>function go() { return 1; }</script></body></html>"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    monkeypatch.setattr(mission_runner, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    yield


def run_plan(scope, plan, page="", goal="Fix the buttons on this preview", budget=600):
    thread = int(vault.active_thread(scope, "task_finder")["id"])
    payload = {"goal": goal, "plan": plan, "mode": "normal", "max_tokens": budget}
    if page:
        payload["page"] = page
    job_id = jobs.enqueue(scope, mission_runner.KIND, payload, {}, thread_id=thread)
    jobs.run_job(vault.claim_job("t", (mission_runner.KIND,)))
    return thread, vault.job_view(vault.job_by_id(job_id))


# ----------------------------------------------------------------------------
# Patch mode
# ----------------------------------------------------------------------------

def test_patch_blocks_are_parsed_and_applied():
    answer = (
        "Two edits.\n<<<<<<< SEARCH\n<h1>Big page</h1>\n=======\n<h1>Bigger page</h1>\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n<button id=\"go\">Go</button>\n=======\n<button id=\"go\">Go now</button>\n<p>added</p>\n>>>>>>> REPLACE\nChanged the title and the button."
    )
    edits = pagepatch.parse_edits(answer)
    assert len(edits) == 2
    merged, problems = pagepatch.apply_edits(BIG_PAGE, edits)
    assert problems == [] and "<h1>Bigger page</h1>" in merged and "<p>added</p>" in merged and "Big page</h1>" not in merged
    loose, problems = pagepatch.apply_edits(BIG_PAGE, [("<button  id=\"go\">Go</button>", "<b>x</b>")])
    assert problems == [] and "<b>x</b>" in loose  # spacing differences are forgiven when the match is still unique
    _, problems = pagepatch.apply_edits(BIG_PAGE, [("padding: 4px;", "padding: 8px;"), ("<nav>nope</nav>", "x"), ("", "y")])
    assert len(problems) == 3 and "appears 300 times" in problems[0] and "nothing in the page matches" in problems[1] and "empty" in problems[2]


def test_patch_mode_applies_to_large_pages_unless_a_fresh_page_is_asked():
    assert pagepatch.wants_patch_mode("change the title", BIG_PAGE)
    assert not pagepatch.wants_patch_mode("change the title", PAGE1)
    assert not pagepatch.wants_patch_mode("rewrite the page from scratch", BIG_PAGE)
    assert pagepatch.parse_edits("no blocks here") == []


def test_the_prompt_asks_for_edit_blocks_in_patch_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    messages = build_prompt_messages("u4", "change the title", workspace="chat_bot", current_page=BIG_PAGE, interface=True, patch_mode=True)
    assert "PATCH MODE" in messages[0]["content"] and "<<<<<<< SEARCH" in messages[0]["content"]
    assert messages[-1]["content"].startswith(pagepatch.CURRENT_PAGE_HEADER_PATCH)
    plain = build_prompt_messages("u4", "change the title", workspace="chat_bot", current_page=PAGE1, interface=True)
    assert "PATCH MODE" not in plain[0]["content"]


# ----------------------------------------------------------------------------
# Missions
# ----------------------------------------------------------------------------

def test_preview_missions_are_classified_with_check_and_repair_steps():
    assert missions.classify_mission("Fix the buttons on this preview") == "preview"
    plan = missions.normalise_plan(missions.task_plan("Fix the buttons on this preview", 3))
    assert [step["executor"] for step in plan] == ["preview.validate", "preview.repair", "preview.validate"]
    assert missions.classify_mission("write an essay about rivers") == "writing"


def test_validate_reports_the_static_verdict_and_a_failed_step_posts_in_the_thread(db):
    plan = missions.normalise_plan([{"title": "Check", "executor": "preview.validate", "type": "quick_text", "description": "check"}])
    thread, view = run_plan("u4-val", plan, page=BROKEN_PAGE)
    assert view["status"] == "done" and view["result"]["succeeded"] == 1
    assert view["result"]["page_check"]["complete"] is False
    rows = vault.recent_messages("u4-val", 20, thread_id=thread)
    report = next(r["content"] for r in rows if r["role"] == "assistant")
    assert "INCOMPLETE" in report and "cdn.tailwindcss.com" in report and "no browser" in report
    # No page at all: the step fails and the thread says so.
    thread, view = run_plan("u4-nopage", plan)
    assert view["result"]["failed"] == 1
    rows = vault.recent_messages("u4-nopage", 20, thread_id=thread)
    system = [r for r in rows if r["role"] == "system"]
    assert system and system[0]["content"].startswith("Step 1 (Check) failed: ") and system[0]["finish"] == "failed"


def test_repair_loops_until_the_static_check_passes_and_the_page_travels_back(db, monkeypatch):
    calls = []

    def fake_generate(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None, **kwargs):
        calls.append((messages, kwargs))
        text = fenced(BROKEN_PAGE) if len(calls) == 1 else fenced(GOOD_PAGE)
        return text, RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setattr(mission_runner, "generate_mode", fake_generate)
    plan = missions.normalise_plan(missions.task_plan("Fix the buttons on this preview", 3))
    thread, view = run_plan("u4-repair", plan, page=BROKEN_PAGE)
    result = view["result"]
    assert view["status"] == "done" and result["succeeded"] == 3 and result["failed"] == 0, result
    assert len(calls) == 2 and calls[0][1] == {"interface": True}
    system = calls[0][0][0]["content"]
    assert "mission step" in system and "CANVAS RULES" in system
    assert "CURRENT PAGE" in calls[0][0][-1]["content"] and BROKEN_PAGE in calls[0][0][-1]["content"]
    assert result["page_complete"] is True and result["page_check"]["complete"] is True
    _, body = vault.export_artifact(int(result["page_artifact"]), "u4-repair")
    assert body == GOOD_PAGE


def test_a_full_window_is_a_pause_not_a_failed_step(db, monkeypatch):
    calls = {"n": 0}

    def flaky(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("Cortex 2 found no BYOK endpoint with headroom -> groq: 30/30 requests used in the last minute")
        return "done", RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setattr(mission_runner, "generate_mode", flaky)
    monkeypatch.setattr(mission_runner, "headroom_wait_seconds", lambda *a, **k: 0.01)
    plan = missions.normalise_plan([{"title": "One", "executor": "model", "type": "chat", "description": "say done"}])
    _, view = run_plan("u4-wait", plan, goal="say done")
    assert view["status"] == "done" and view["result"]["succeeded"] == 1 and calls["n"] == 2


# ----------------------------------------------------------------------------
# The app
# ----------------------------------------------------------------------------

def patch_provider(monkeypatch, answer_text):
    calls = []

    def answer(task_type, messages, *args, **kwargs):
        calls.append(messages)
        return answer_text, RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-u4")
    monkeypatch.setattr(router, "heavy_stream", answer)
    monkeypatch.setattr(router, "generate_mode", lambda mode, task_type, messages, *a, **k: answer(task_type, messages))
    monkeypatch.setattr(router, "cortex_wait_seconds", lambda *a, **k: 0.0)
    monkeypatch.setattr(proctor, "cached_fragility", lambda *a, **k: None)
    return calls


def test_a_large_page_is_edited_with_search_replace(app, monkeypatch):
    calls = patch_provider(monkeypatch, "<<<<<<< SEARCH\n<h1>Big page</h1>\n=======\n<h1>Renamed</h1>\n>>>>>>> REPLACE\nRenamed the heading.")
    vault.initialize_database()
    vault.setting_set("u4-patch", "preview_state", json.dumps({"source": BIG_PAGE, "mode": "Run the page", "complete": True, "reasons": []}))
    app.query_params["scope"] = "u4-patch"
    app.query_params["ws"] = "chat_bot"
    app.run()
    assert app.session_state["preview_source"] == BIG_PAGE
    app.chat_input[0].set_value("change the heading of this page to Renamed").run()
    assert not app.exception
    assert "PATCH MODE" in calls[0][0]["content"]
    assert app.session_state["preview_source"] == BIG_PAGE.replace("<h1>Big page</h1>", "<h1>Renamed</h1>")
    assert any(c == "Applied 1 of 1 edit(s) to the page on the canvas." for c in captions(app)), captions(app)


def test_task_finder_says_it_cannot_run_the_browser_and_offers_chat_bot(app, monkeypatch):
    calls = patch_provider(monkeypatch, "Here is the fixed page.")
    vault.initialize_database()
    vault.setting_set("u4-tf", "preview_state", json.dumps({"source": PAGE1, "mode": "Run the page", "complete": True, "reasons": []}))
    app.query_params["scope"] = "u4-tf"
    app.query_params["ws"] = "task_finder"
    app.run()
    app.chat_input[0].set_value("Fix the buttons on this preview").run()
    assert not app.exception and calls == []  # a mission is proposed, nothing runs
    assert any("Missions cannot run the browser" in i.value for i in app.info), [i.value for i in app.info]
    thread_id = int(vault.active_thread("u4-tf", "task_finder")["id"])
    app.button(key=f"to_chat_bot_{thread_id}").click().run()
    assert not app.exception
    assert app.session_state["workspace_select"] == "chat_bot" and "queued_send" not in app.session_state
    assert len(calls) == 1 and calls[0][-1]["content"].endswith("Fix the buttons on this preview")
    assert "CURRENT PAGE" in calls[0][-1]["content"]  # the canvas page went with it
    assert [r["role"] for r in vault.recent_messages("u4-tf", 10, workspace="chat_bot")] == ["user", "assistant"]
