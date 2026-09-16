"""Batch U1 (stop the spiral): a cut answer is continued and remembered, pages route to endpoints that can write them,
memory serves the page instead of outranking it, and the canvas keeps one whole page."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import preview, prompting, router, sandbox_preview, skills, vault  # noqa: E402
from orchestrator.router import RouteDecision  # noqa: E402

F = "`" * 3
PAGE = "<!doctype html>\n<html>\n<head><title>t</title></head>\n<body>\n<button id=\"go\">Go</button>\n<script>\nlet n = 0;\ndocument.getElementById('go').onclick = () => { n += 1; };\n</script>\n</body>\n</html>"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    vault.initialize_database()
    return "scope-u"


# --- router: interface requests, budgets, continuation, answer shape ---------------------------------------------

def test_interface_requests_and_output_budget(monkeypatch):
    assert router.is_interface_request("make me a study app with 100 questions") and router.is_interface_request("the buttons on the preview do nothing")
    assert not router.is_interface_request("summarise this email in three lines")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    messages = [{"role": "user", "content": "x" * 4_000}]
    assert router.effective_output_budget(messages, 2_048, interface=False) == 2_048
    raised = router.effective_output_budget(messages, 2_048, interface=True)
    assert raised == router.INTERFACE_OUTPUT_CAP  # Gemini can write it; the cap, not the slider, bounds a page
    assert router.effective_output_budget(messages, 8_192, interface=True) >= 8_192
    monkeypatch.delenv("GEMINI_API_KEY")
    groq_only = router.effective_output_budget(messages, 2_048, interface=True)
    assert 2_048 <= groq_only < router.INTERFACE_OUTPUT_CAP  # Groq's 8k minute window bounds what it can write
    assert router.cannot_write(10_000) == {"huggingface"} and router.cannot_write(0) == set()


def test_stitch_and_needs_continuation():
    cut = F + "html\n<html><body><p>Hello wor"
    assert router.needs_continuation(cut, "length") and not router.needs_continuation(cut, "stop") and not router.needs_continuation(cut + F, "length")
    assert router.stitch(cut, "ld</p></body></html>\n" + F) == cut + "ld</p></body></html>\n" + F
    # A repeated tail and a repeated fence opener are dropped, not doubled.
    tail = "<p>Hello wor"
    assert router.stitch(cut, F + "html\n" + tail + "ld</p>") == cut + "ld</p>"
    assert router.stitch(cut, "<body><p>Hello world</p>") == F + "html\n<html><body><p>Hello world</p>"


def test_continue_answer_finishes_a_cut_page_in_normal_mode(monkeypatch):
    pieces = iter([("ld</p>", "length"), ("</body></html>\n" + F, "stop")])
    seen = []

    def fake_generate(task_type, messages, ledger=None, max_tokens=4096, temperature=0.2, system_prompt="", output_need=0):
        seen.append((messages[-1]["content"], output_need))
        text, finish = next(pieces)
        return text, RouteDecision("fake", "m", task_type, "r", finish=finish)

    monkeypatch.setattr(router, "cortex_generate", fake_generate)
    cut = F + "html\n<html><body><p>Hello wor"
    text, decision, used = router.continue_answer("chat", [{"role": "user", "content": "page please"}], cut, RouteDecision("fake", "m", "chat", "first", finish="length"), None, 4096)
    assert text == F + "html\n<html><body><p>Hello world</p></body></html>\n" + F and used == 2 and decision.finish == "stop"
    assert "continued 2x" in decision.reason and all(need == 4096 for _, need in seen)
    assert seen[0][0].startswith("CONTINUATION: your previous answer was cut") and "[TAIL]" in seen[0][0]
    # Bounded: a page that never closes stops after CONTINUATION_ROUNDS.
    endless = iter([("x", "length")] * 10)
    monkeypatch.setattr(router, "cortex_generate", lambda *a, **k: (next(endless)[0], RouteDecision("fake", "m", "chat", "r", finish="length")))
    _, decision, used = router.continue_answer("chat", [{"role": "user", "content": "p"}], cut, RouteDecision("fake", "m", "chat", "r", finish="length"), None, 4096)
    assert used == router.CONTINUATION_ROUNDS and decision.finish == "length"


def test_answer_shape_problems_name_the_garbage():
    assert "fragment" in router.answer_shape_problem('radio" name="era-tab" id="tab-classical" class="era-tab-radio">')
    assert "deliberation" in router.answer_shape_problem("\n" + F + "\n\nWait, let's make sure the code is never cut off. Let's double-check the total token count.")
    assert router.answer_shape_problem("Here is the page:\n" + F + "html\n<html></html>\n" + F) == ""
    assert router.answer_shape_problem("The tide follows the moon because gravity...") == ""
    assert router.answer_shape_problem("") == "empty"


def test_request_body_lowers_gpt_oss_reasoning_only_for_page_budgets(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    _, _, small = router.build_cortex_request("groq", [{"role": "user", "content": "hi"}], 2_048, 0.2, False, model_id="openai/gpt-oss-120b")
    _, _, page = router.build_cortex_request("groq", [{"role": "user", "content": "hi"}], 8_192, 0.2, False, model_id="openai/gpt-oss-120b")
    assert "reasoning_effort" not in small and page["reasoning_effort"] == "low" and page["include_reasoning"] is False


def test_heavy_pipeline_gives_a_page_the_whole_budget_and_keeps_the_cut_on_fallbacks():
    budgets = []

    def one_pass(task_type, messages, tokens):
        budgets.append((task_type, tokens))
        if task_type == "reasoning":
            raise router.ProviderError("critique window full")
        return F + "html\n<p>cut", RouteDecision("fake", "m", task_type, "draft", finish="length")

    text, decision = router._heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "make a page"}], 4_096, interface=True)
    assert budgets[0] == ("chat", 4_096) and decision.finish == "length" and text.startswith(F + "html")
    budgets.clear()
    router._heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "explain tides"}], 4_096)
    assert budgets[0] == ("chat", 2_048)  # a plain answer keeps the half-budget draft

    critiques = []

    def with_critique(task_type, messages, tokens):
        if task_type == "reasoning":
            critiques.append(messages[0]["content"])
            return "- incomplete", RouteDecision("fake", "m", task_type, "c")
        if "[CANDIDATE ANSWER]" not in messages[-1]["content"]:
            return F + "html\n<p>cut", RouteDecision("fake", "m", task_type, "draft", finish="length")
        assert "COMPLETE page" in messages[0]["content"] and "never shorter" in messages[0]["content"]
        return "final", RouteDecision("fake", "m", task_type, "final")

    text, _ = router._heavy_pipeline(with_critique, "chat", [{"role": "user", "content": "make a page"}], 4_096, interface=True)
    assert text == "final" and "not where a design ended" in critiques[0]


# --- vault: the cut note, memory order, decisions from the operator only, recall words, health ---------------------

def test_a_cut_answer_carries_its_note_into_the_next_prompt(db):
    vault.append_message(db, "user", "make a page", workspace="normal_chat")
    vault.append_message(db, "assistant", F + "html\n<html><body><p>half", workspace="normal_chat", finish="length")
    parts = vault.context_parts(db, workspace="normal_chat")
    turn = parts["turns"][-1]["content"]
    assert "[CUT AT THE OUTPUT BUDGET" in turn and "half'" in turn and "Do not shrink" in turn
    rows = vault.recent_messages(db, 5, workspace="normal_chat")
    assert rows[-1]["finish"] == "length" and rows[0]["finish"] == ""


def test_live_window_is_filled_before_the_digest_and_the_digest_is_background(db):
    thread = vault.create_thread(db, "parent", workspace="normal_chat")
    for i in range(6):
        vault.append_message(db, "user", f"we must always use blue buttons {i}", thread_id=thread, workspace="normal_chat")
        vault.append_message(db, "assistant", "Inline onclick attributes are blocked by CSP in sandboxed previews; scripts may be stripped entirely. " * 3, thread_id=thread, workspace="normal_chat")
    result = vault.migrate_thread(db, thread_id=thread)
    successor = int(result["new_thread_id"])
    digest = str(result["digest"])
    assert "we must always use blue buttons" in digest and "blocked by CSP" not in digest  # only the operator's sentences are decisions
    big = "<html><body>" + ("<p>row</p>\n" * 1_500) + "</body></html>"
    vault.append_message(db, "user", "the buttons do not work", thread_id=successor, workspace="normal_chat")
    vault.append_message(db, "assistant", big, thread_id=successor, workspace="normal_chat")
    parts = vault.context_parts(db, max_characters=24_000, thread_id=successor)
    assert parts["memory"].startswith("[BACKGROUND from chat #") and "never a rule" in parts["memory"]
    page_turn = parts["turns"][-1]["content"]
    assert page_turn == big  # the whole page survived; the digest took at most an eighth of the window
    assert len(parts["memory"]) <= 24_000 // 8 + 200
    # Even a page bigger than the window keeps most of it: memory is squeezed to its floor, not the page.
    huge = "<html><body>" + ("<p>row</p>\n" * 3_000) + "</body></html>"
    vault.append_message(db, "user", "bigger", thread_id=successor, workspace="normal_chat")
    vault.append_message(db, "assistant", huge, thread_id=successor, workspace="normal_chat")
    parts = vault.context_parts(db, max_characters=24_000, thread_id=successor)
    assert len(parts["turns"][-1]["content"]) >= 24_000 - 24_000 // 8 - 400


def test_recall_ignores_stopwords_and_needs_whole_words(db):
    assert vault.recall_words("the buttons don't work in the preview") == ["buttons", "preview"]
    assert vault.recall_words("t in the") == []
    other = vault.create_thread(db, "other", workspace="normal_chat")
    for _ in range(3):
        vault.append_message(db, "user", "why do the buttons in the preview do nothing", thread_id=other, workspace="normal_chat")
        vault.append_message(db, "assistant", "Inline onclick attributes are blocked by CSP in sandboxed previews; scripts may be stripped entirely.", thread_id=other, workspace="normal_chat")
    vault.migrate_thread(db, thread_id=other)
    fresh = vault.create_thread(db, "fresh", workspace="chat_bot")
    recalled = vault.recall_memory(db, "the buttons don't work in the preview", fresh, 2_000)
    assert "blocked by CSP" not in recalled  # the model's diagnosis never comes back; the operator's own question may
    assert vault.recall_memory(db, "the buttons don't work in the sandboxed previews", fresh, 2_000).count("CSP") == 0


def test_thread_health_ignores_automatic_turns_and_migrates_much_later(db):
    thread = vault.create_thread(db, "t", workspace="normal_chat")
    for _ in range(4):
        vault.append_message(db, "user", "AUTOMATIC FIX 1/2: " + "x" * 20_000, thread_id=thread, workspace="normal_chat")
        vault.append_message(db, "assistant", "<p>page</p>", thread_id=thread, workspace="normal_chat")
    health = vault.thread_health(db, thread_id=thread)
    assert health["tokens"] < 1_000 and not health["recommend_migration"]
    assert vault.HEALTH_TOKEN_LIMIT == 60_000 and vault.MIN_MESSAGES_AFTER_MIGRATION == 40


# --- prompting: app state, canvas rules, the current page, no recall for pages ---------------------------------------

def test_prompt_carries_app_state_canvas_rules_and_the_current_page(db):
    messages = prompting.build_prompt_messages(
        db, "fix the buttons on this page", workspace="normal_chat", app_state="APP STATE: canvas mode Run", current_page="<html><body><button>x</button></body></html>",
    )
    system = messages[0]["content"]
    assert "APP STATE: canvas mode Run" in system and "CANVAS RULES" in system and "no CDN" in system
    assert system.index("APP STATE") < system.index("CANVAS RULES")
    request = messages[-1]["content"]
    assert request.startswith(prompting.CURRENT_PAGE_HEADER) and "CURRENT REQUEST:\nfix the buttons on this page" in request
    plain = prompting.build_prompt_messages(db, "explain tides", workspace="normal_chat")
    assert "CANVAS RULES" not in plain[0]["content"] and "APP STATE" not in plain[0]["content"]


def test_skills_stay_in_their_workspace_and_never_ride_an_automatic_turn():
    assert [s.name for s in skills.select_skills("patch the repository diff and open a pull request", workspace="normal_chat")] == []
    assert [s.name for s in skills.select_skills("patch the repository diff and open a pull request", workspace="repository")] == ["repository-patching"]
    assert skills.select_skills("keep what works fix the broken things one at a time", workspace="chat_bot") == []
    assert skills.select_skills("AUTOMATIC FIX 1/2: the page did not work; patch the repository", workspace="repository") == []
    assert skills.skills_block("the ceo scorecard and the l10 agenda", workspace="normal_chat") == ("", [])
    assert skills.skills_block("the ceo scorecard and the l10 agenda", workspace="company")[1] == ["company-reporting"]


# --- preview: which fence wins, completeness ---------------------------------------------------------------------------

def test_the_last_whole_page_wins_over_snippets_and_stubs():
    snippet = F + "html\n<button>patch me</button>\n" + F
    full = F + "html\n" + PAGE + "\n" + F
    css = F + "css\nbody { color: red }\n" + F
    assert preview.extract_preview_source(snippet + "\n\n" + full) == PAGE
    assert preview.extract_preview_source(css + "\n\n" + full) == PAGE
    partial = F + "html\n<!doctype html><html><body><p>first try"
    assert preview.extract_preview_source(partial + "\n" + F + "\n" + full) == PAGE
    source, closed = preview.extract_preview_fence("Here:\n" + F + "html\n" + PAGE[:120])
    assert source.startswith("<!doctype html>") and closed is False
    assert preview.extract_preview_source(css) == preview._fence_source("css", "body { color: red }\n")
    assert preview.extract_preview_source(snippet) == "<button>patch me</button>"


def test_page_completeness_names_every_reason():
    assert preview.page_completeness(PAGE) == []
    cut = PAGE.split("<script>")[0] + "<script>\nlet n = 0;\ndocument.getElementById('go').onclick = () => { n += 1;"
    reasons = preview.page_completeness(cut, closed=False, finish="length")
    assert reasons[0] == "the answer was cut at the length limit" and "the code fence never closed" in reasons
    assert "</html> is missing" in reasons and "a script block never closes" in reasons
    unbalanced = PAGE.replace("{ n += 1; }", "{ n += 1;")
    assert preview.page_completeness(unbalanced) == ["a script has unbalanced braces or an unterminated string"]
    assert preview.page_completeness("<script>const s = \"a } b\"; const t = `x { y`; // }\n</script>") == []


def test_an_incomplete_page_is_continued_not_repaired():
    report = sandbox_preview.normalize_report({"seq": 1, "status": "error", "errors": [{"message": "Unexpected end of input", "line": 9}]})
    state = {"rounds": 0, "last_signature": "", "handled_seq": 0}
    allowed, reason, settled = sandbox_preview.fix_decision(state, report, True, False, complete=False)
    assert not allowed and settled and reason == sandbox_preview.INCOMPLETE_REASON
    assert sandbox_preview.fix_decision(state, report, True, False, complete=True)[0] is True
