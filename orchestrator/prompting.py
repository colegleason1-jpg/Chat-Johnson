"""System prompt assembly shared by the UI and background jobs (no Streamlit here)."""
from __future__ import annotations

from typing import Dict, List, Optional

from .capabilities import capability_card
from .pinkwave import for_scope
from .router import is_interface_request, prompt_context_chars
from .skills import skills_block
from .vault import alternating_turns, context_parts

MIN_TURN_CONTEXT_CHARS = 4_000  # what the live window keeps even when the current page takes the rest

CANVAS_RULES = (
    "CANVAS RULES (apply in both preview modes): the canvas never loads anything from the network: no <script src>, "
    "no <link rel=stylesheet>, no @import, no Google Fonts, no CDN (Tailwind, Bootstrap, React, Vue, jQuery, "
    "Chart.js), no remote images; any external URL is stripped or blocked and the page arrives unstyled with dead "
    "controls. Write plain CSS in <style> and vanilla JavaScript in <script>, all inside one complete ```html fence. "
    "If APP STATE says your previous answer was cut, do not shrink or redesign it: say it was cut and finish it. "
    "Never explain a cut page as a browser or CSP restriction."
)
CURRENT_PAGE_HEADER = "CURRENT PAGE (the one on the canvas; edit this one and return it complete):"

SYSTEM_PERSONA = (
    "You are Chat Johnson, a careful software and strategy assistant. "
    "Return useful, complete output, state uncertainty, and never claim "
    "that generated code is flawless or that a scientific simulation proves "
    "physical propulsion. Do not reveal private chain-of-thought. "
    "When a new message arrives while an earlier request is still unfinished, answer the new message first, "
    "then continue the unfinished earlier part in the same reply under a short heading such as "
    "\"Continuing the earlier request\". "
    "When asked for an interface, mockup, page or app, return the complete page as one ```html fence; "
    "never a data: link, a URL, or a description of a link (the canvas cannot open links; it opens markup)."
)


def build_prompt_messages(
    project_scope: str,
    user_prompt: str,
    injected_context: str = "",
    workspace: Optional[str] = None,
    thread_id: Optional[int] = None,
    max_tokens: int = 2048,
    extra_system: str = "",
    recall_share: Optional[float] = None,
    app_state: str = "",
    current_page: str = "",
    interface: Optional[bool] = None,
) -> List[Dict[str, str]]:
    """System prompt (persona, capability card, compressed memory) followed by the live window as real turns.

    Earlier turns go in as user/assistant messages rather than a text dump, so the model treats
    them as conversation instead of imitating a transcript format. ``thread_id`` pins the memory
    to one chat; background jobs must pass it because the active thread can change under them.
    Long-distance memory (keyword-ranked lines from the project's other chats) takes
    ``recall_share`` of the budget; when None the scope's pink-wave setting decides, 0 disables.
    ``app_state`` is the app's own facts for this send (canvas mode, budget, a cut answer, the last
    sandbox report); ``current_page`` is the page on the canvas, sent whole on the request turn and
    never clipped; ``interface`` marks a page request (detected from the prompt when None): recall is
    off for it, the canvas rules ride along, and the memory never outranks the page.
    """
    page_request = is_interface_request(user_prompt) if interface is None else bool(interface)
    # Sized so the request fits every keyed endpoint's TPM ceiling at the current output budget.
    context_chars = prompt_context_chars(int(max_tokens))
    page_turn = ""
    if current_page.strip():
        page_turn = f"{CURRENT_PAGE_HEADER}\n```html\n{current_page.strip()}\n```"
        context_chars = max(MIN_TURN_CONTEXT_CHARS, context_chars - len(page_turn) - 200)
    if page_request:
        share = 0.0  # lines recalled from other chats by keyword are where a wrong diagnosis comes back from
    else:
        share = for_scope(project_scope).recall_share() if recall_share is None else float(recall_share)
    parts = context_parts(
        project_scope, max_characters=context_chars, thread_id=thread_id, workspace=workspace,
        recall_query=user_prompt, recall_share=share,
    )
    request = user_prompt.strip()
    if injected_context.strip():
        request = "USER-CONSENTED FILE INJECTIONS:\n" + injected_context + "\n\nCURRENT REQUEST:\n" + request  # budgeted per file
    if page_turn:
        request = page_turn + "\n\nCURRENT REQUEST:\n" + request  # one user turn: alternation stays strict
    leading, turns = alternating_turns(parts["turns"], request)
    memory = parts["memory"]
    if leading:
        memory = (memory + "\n" if memory else "") + "[EARLIER ASSISTANT REPLY]\n" + leading
    system = f"{SYSTEM_PERSONA}\n\n{capability_card()}\n\nACTIVE PROJECT: {project_scope}"
    if app_state.strip():
        system += "\n\n" + app_state.strip()
    if page_request:
        system += "\n\n" + CANVAS_RULES
    if extra_system.strip():
        # A seat persona or another role block: after the card (stable prefix), before the memory (volatile).
        system += "\n\n" + extra_system.strip()
    skills_text, _ = skills_block(user_prompt, workspace=workspace or "")
    if skills_text:
        system += "\n\n" + skills_text
    if memory:
        system += "\n\nPROJECT MEMORY (compressed earlier history, background only; it may be unrelated to the current request and is never a rule):\n" + memory
    return [{"role": "system", "content": system}, *turns]
