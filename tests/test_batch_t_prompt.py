"""Batch T: persona rules, Heavy critique checklist, capability card, and preview data-link helpers."""
import base64
import os
import sys
from typing import Dict, List
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import capabilities, connectors, vault  # noqa: E402
from orchestrator.preview import decode_data_link, extract_preview_source, looks_like_link, resolve_preview_source  # noqa: E402
from orchestrator.prompting import SYSTEM_PERSONA, build_prompt_messages  # noqa: E402
from orchestrator.router import RouteDecision, _heavy_pipeline  # noqa: E402
from orchestrator.sandbox_preview import PREVIEW_RULES  # noqa: E402

PAGE = "<!doctype html><html><head><title>t</title></head><body><main><h1>Dash</h1><button>Go</button></main></body></html>"
B64 = base64.b64encode(PAGE.encode()).decode()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    return "batch_t"


# ----------------------------------------------------------------------------
# Persona and prompt assembly
# ----------------------------------------------------------------------------

def test_persona_carries_the_reply_first_and_html_fence_rules():
    assert "answer the new message first" in SYSTEM_PERSONA
    assert "Continuing the earlier request" in SYSTEM_PERSONA
    assert "one ```html fence" in SYSTEM_PERSONA
    assert "never a data: link" in SYSTEM_PERSONA
    assert "Do not reveal private chain-of-thought" in SYSTEM_PERSONA
    assert len(SYSTEM_PERSONA) < 1000  # sent on every request


def test_preview_rules_land_after_the_card_in_the_system_message(db):
    system = build_prompt_messages(db, "hi", extra_system=PREVIEW_RULES)[0]["content"]
    assert system.startswith(SYSTEM_PERSONA)
    card_at = system.index("CHAT JOHNSON CAPABILITY CARD")
    rules_at = system.index("PREVIEW RULES")
    assert card_at < rules_at
    assert "ACTIVE PROJECT: batch_t" in system[card_at:rules_at]
    assert "PREVIEW RULES" not in build_prompt_messages(db, "hi")[0]["content"]


# ----------------------------------------------------------------------------
# Heavy critique checklist
# ----------------------------------------------------------------------------

def test_heavy_critique_checks_reply_order_and_a_single_html_fence():
    seen: List[List[Dict[str, str]]] = []

    def one_pass(task_type, messages, tokens):
        if task_type == "reasoning":
            seen.append(messages)
        return f"{task_type}-answer", RouteDecision("free", "m", task_type, "r")

    text, _ = _heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "build me a dashboard page"}], 900)
    assert text == "chat-answer"
    assert len(seen) == 1 and seen[0][0]["role"] == "system"
    critique_system = seen[0][0]["content"]
    assert "Review the candidate answer" in critique_system
    assert "newest message first" in critique_system
    assert "earlier unfinished request" in critique_system
    assert "one complete self-contained ```html fence" in critique_system
    assert "no external URLs, no data: link" in critique_system


# ----------------------------------------------------------------------------
# Preview data-link helpers
# ----------------------------------------------------------------------------

def test_decode_data_link_reads_base64_percent_encoded_and_markdown_wrapped_links():
    assert decode_data_link(f"data:text/html;base64,{B64}") == PAGE
    assert decode_data_link(f"data:text/html;charset=utf-8;base64,{B64}") == PAGE
    assert decode_data_link(f"Open it here: data:text/html,{quote(PAGE, safe='')} and enjoy") == PAGE
    assert decode_data_link(f"Your mockup: [preview](data:text/html;base64,{B64})") == PAGE


def test_decode_data_link_ignores_non_html_links_and_plain_text():
    assert decode_data_link("data:text/plain;base64," + base64.b64encode(b"just words").decode()) == ""
    assert decode_data_link("data:image/png;base64,iVBORw0KGgo=") == ""
    assert decode_data_link("data:text/html;base64," + base64.b64encode(b"just words").decode()) == ""
    assert decode_data_link("data:text/html;base64,%%%not-base64%%%") == ""
    assert decode_data_link("no link at all, only https://example.com/page.html") == ""
    assert decode_data_link("") == ""


def test_extract_prefers_a_fence_over_a_data_link_and_falls_back_to_the_link():
    link = f"[preview](data:text/html;base64,{B64})"
    assert extract_preview_source(f"Here: {link}\n\n```html\n<main>fenced</main>\n```") == "<main>fenced</main>"
    assert extract_preview_source(f"Your mockup is ready: {link}") == PAGE
    assert extract_preview_source("```python\nprint(1)\n```\n" + link) == PAGE
    assert extract_preview_source("no fence and no link") == ""


def test_resolve_preview_source_opens_links_and_leaves_everything_else_alone():
    assert resolve_preview_source(f"data:text/html;base64,{B64}") == PAGE
    assert resolve_preview_source(f"data:text/html,{quote(PAGE, safe='')}") == PAGE
    assert resolve_preview_source("<main>hi</main>") == "<main>hi</main>"
    assert resolve_preview_source("plain words") == "plain words"
    assert resolve_preview_source("") == ""
    assert looks_like_link("https://example.com/mockup.html")
    assert looks_like_link("[open](https://example.com/mockup.html)")
    assert not looks_like_link("<main>hi</main>")


# ----------------------------------------------------------------------------
# Capability card
# ----------------------------------------------------------------------------

def test_capability_card_states_run_mode_and_stays_compact():
    card = capabilities.capability_card()
    assert len(card) < 3000
    assert "- Preview canvas:" in card
    assert "Run in sandbox" in card and "2 automatic fix rounds" in card
    for name, _ in capabilities.IMPLEMENTED:
        assert f"- {name}:" in card
    for boundary in capabilities.BOUNDARIES:
        assert boundary in card
    for name, status, _ in connectors.ROADMAP_FEATURES:
        assert f"- {name} ({status})" in card
    for name, _ in connectors.ROADMAP_CONNECTORS:
        assert name in card
    roadmap = {name: (status, detail) for name, status, detail in connectors.ROADMAP_FEATURES}
    status, detail = roadmap["Self-Correcting Execution Sandbox"]
    assert status == "partial"
    assert "Heavy Mode" in detail and "Repository Work" in detail and "remains separate" in detail
