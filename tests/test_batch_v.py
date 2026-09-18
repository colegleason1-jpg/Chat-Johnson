"""Batch V: the three fixes the measured audit put first.

1. Page mode is a property of the chat, not of the wording of one message, so a follow-up edit still carries the
   page, the canvas rules and the raised budget. The audit measured the old rule attaching the page 0 times in 13
   realistic edit turns.
2. The wait calculation applies the output ceiling, so an endpoint that cannot write the answer stops reporting
   "no wait" and hiding the real one. The audit measured 0.0 s returned where the only capable endpoint needed 60.
3. On a page request the Heavy critique is a deterministic page review instead of a provider call, so the
   per-minute window goes to writing the page rather than to a verdict that gets discarded.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import preview, router, vault  # noqa: E402
from orchestrator.quota import QuotaLedger  # noqa: E402
from orchestrator.router import RouteDecision  # noqa: E402
from tests.test_batch_t_ui import PAGE1, app as app_fixture, fake_provider, fenced  # noqa: E402
from tests.test_cortex import all_keys as keys_fixture  # noqa: E402

app = app_fixture
all_keys = keys_fixture

CUT_PAGE = "<!doctype html><html><head><title>Cut</title></head><body><h1>Half</h1><script>function go() {"
CDN_PAGE = '<!doctype html><html><head><script src="https://cdn.tailwindcss.com"></script></head><body><h1>Hi</h1></body></html>'


# ----------------------------------------------------------------------------
# 2. The wait calculation applies the output ceiling
# ----------------------------------------------------------------------------

def test_an_endpoint_that_cannot_write_the_answer_stops_hiding_the_real_wait(all_keys):
    messages = [{"role": "user", "content": "make me a page"}]
    ledger = QuotaLedger({})
    router._ensure_cortex_ledger(ledger)
    # Gemini is the only endpoint that can write 16k tokens; spend its whole minute window.
    ledger.record("gemini", 31_000)
    plain = router.cortex_wait_seconds(ledger, messages, 16_384)
    aware = router.cortex_wait_seconds(ledger, messages, 16_384, output_need=16_384)
    assert plain == 0.0, "without the output need, an endpoint that cannot write the page reports no wait"
    assert aware > 0.0, "with it, the real wait of the only capable endpoint surfaces"
    exc = router.ProviderError("Cortex 2 found no BYOK endpoint with headroom -> groq: 30/30 requests used in the last minute")
    assert router.headroom_wait_seconds(exc, ledger, messages, 16_384, 65.0) == 0.0
    assert router.headroom_wait_seconds(exc, ledger, messages, 16_384, 65.0, output_need=16_384) > 0.0
    # A small answer is unaffected: every endpoint can write it, so the fastest window still wins.
    assert router.cortex_wait_seconds(ledger, messages, 256, output_need=256) == 0.0


# ----------------------------------------------------------------------------
# 3. The page critique costs nothing
# ----------------------------------------------------------------------------

def test_page_review_names_what_is_wrong_and_costs_no_call():
    complete = preview.page_review(PAGE1)
    assert "structurally complete" in complete and "do not shorten" in complete
    cut = preview.page_review(CUT_PAGE, closed=False, finish="length")
    assert "Problems that must be fixed" in cut and "```html" in cut
    cdn = preview.page_review(CDN_PAGE)
    assert "from the internet" in cdn and "cdn.tailwindcss.com" in cdn
    assert "no complete ```html page" in preview.page_review("")
    assert preview.external_resources(CDN_PAGE) and preview.external_resources(PAGE1) == []


def test_a_heavy_page_request_spends_no_call_on_the_critique():
    calls = []

    def one_pass(task_type, messages, tokens, temperature=None):
        calls.append({"task_type": task_type, "tokens": tokens})
        text = fenced(CUT_PAGE) if len(calls) == 1 else fenced(PAGE1)
        return text, RouteDecision("fake", "m", task_type, "r", finish="length" if len(calls) == 1 else "")

    answer, decision = router._heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "make me a page"}], 16_384, interface=True)
    assert [c["task_type"] for c in calls] == ["chat", "chat"], "draft and synthesis only; the critique made no call"
    assert calls[0]["tokens"] == 16_384  # the draft still gets the whole budget
    assert "review(local-check)" in decision.reason and PAGE1 in answer
    # A non-page request keeps the model critique, on the reasoning route.
    calls.clear()
    router._heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "explain tides"}], 2_048, interface=False)
    assert [c["task_type"] for c in calls] == ["chat", "reasoning", "chat"]


def test_the_synthesis_is_told_what_the_review_found():
    seen = []

    def one_pass(task_type, messages, tokens, temperature=None):
        seen.append(messages)
        return fenced(CUT_PAGE), RouteDecision("fake", "m", task_type, "r")

    router._heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "make me a page"}], 8_192, interface=True)
    synthesis = "\n".join(str(m.get("content", "")) for m in seen[-1])
    assert "PAGE REVIEW" in synthesis and "no provider call" in synthesis


# ----------------------------------------------------------------------------
# 1. Page mode sticks to the chat
# ----------------------------------------------------------------------------

def seed_canvas(scope: str, page: str = PAGE1) -> None:
    vault.initialize_database()
    vault.setting_set(scope, "preview_state", json.dumps({"source": page, "mode": "Run the page", "complete": True, "reasons": []}))


@pytest.mark.parametrize("follow_up", [
    "make the header blue",
    "add a timer at the top",
    "fix the score so it keeps going up",
    "add a review screen at the end",
    "shuffle them",
])
def test_a_follow_up_edit_still_carries_the_page(app, monkeypatch, follow_up):
    calls = fake_provider(monkeypatch, [fenced(PAGE1), fenced(PAGE1)])
    scope = "v-" + follow_up.split()[1]
    seed_canvas(scope)
    app.query_params["scope"] = scope
    app.query_params["ws"] = "normal_chat"
    app.run()
    # A page landed in this chat, so the chat is in page mode from here on.
    app.chat_input[0].set_value("make me a study app with buttons").run()
    assert not app.exception and app.session_state["preview_source"] == PAGE1
    assert json.loads(vault.setting_get(scope, "page_mode"))["normal_chat"] > 0
    app.chat_input[0].set_value(follow_up).run()
    assert not app.exception and len(calls) == 2
    sent = calls[1]
    system = next(m["content"] for m in sent if m["role"] == "system")
    turn = sent[-1]["content"]
    assert "CURRENT PAGE" in turn and PAGE1 in turn, f"{follow_up!r} did not carry the page"
    assert "CANVAS RULES" in system, f"{follow_up!r} did not carry the canvas rules"


def test_clearing_the_canvas_ends_page_mode(app, monkeypatch):
    fake_provider(monkeypatch, [fenced(PAGE1)])
    seed_canvas("v-clear")
    app.query_params["scope"] = "v-clear"
    app.query_params["ws"] = "normal_chat"
    app.run()
    app.chat_input[0].set_value("make me a page").run()
    assert json.loads(vault.setting_get("v-clear", "page_mode")).get("normal_chat")
    app.button(key="toggle_canvas").click().run()  # open the canvas so its Clear button exists
    app.button(key="clear__preview_editor").click().run()
    assert not app.exception
    assert json.loads(vault.setting_get("v-clear", "page_mode")) == {}
    assert app.session_state["preview_source"] == ""
