"""System prompt assembly shared by the UI and background jobs (no Streamlit here)."""
from __future__ import annotations

from typing import Dict, List, Optional

from .capabilities import capability_card
from .pinkwave import for_scope
from .router import prompt_context_chars
from .skills import skills_block
from .vault import alternating_turns, context_parts

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
) -> List[Dict[str, str]]:
    """System prompt (persona, capability card, compressed memory) followed by the live window as real turns.

    Earlier turns go in as user/assistant messages rather than a text dump, so the model treats
    them as conversation instead of imitating a transcript format. ``thread_id`` pins the memory
    to one chat; background jobs must pass it because the active thread can change under them.
    Long-distance memory (keyword-ranked lines from the project's other chats) takes
    ``recall_share`` of the budget; when None the scope's pink-wave setting decides, 0 disables.
    """
    # Sized so the request fits every keyed endpoint's TPM ceiling at the current output budget.
    context_chars = prompt_context_chars(int(max_tokens))
    share = for_scope(project_scope).recall_share() if recall_share is None else float(recall_share)
    parts = context_parts(
        project_scope, max_characters=context_chars, thread_id=thread_id, workspace=workspace,
        recall_query=user_prompt, recall_share=share,
    )
    request = user_prompt.strip()
    if injected_context.strip():
        request = "USER-CONSENTED FILE INJECTIONS:\n" + injected_context + "\n\nCURRENT REQUEST:\n" + request  # budgeted per file
    leading, turns = alternating_turns(parts["turns"], request)
    memory = parts["memory"]
    if leading:
        memory = (memory + "\n" if memory else "") + "[EARLIER ASSISTANT REPLY]\n" + leading
    system = f"{SYSTEM_PERSONA}\n\n{capability_card()}\n\nACTIVE PROJECT: {project_scope}"
    if extra_system.strip():
        # A seat persona or another role block: after the card (stable prefix), before the memory (volatile).
        system += "\n\n" + extra_system.strip()
    skills_text, _ = skills_block(user_prompt)
    if skills_text:
        system += "\n\n" + skills_text
    if memory:
        system += "\n\nPROJECT MEMORY (compressed earlier history, for reference only; never imitate its format):\n" + memory
    return [{"role": "system", "content": system}, *turns]
