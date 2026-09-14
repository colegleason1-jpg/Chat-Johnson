"""System prompt assembly shared by the UI and background jobs (no Streamlit here)."""
from __future__ import annotations

from typing import Dict, List, Optional

from .capabilities import capability_card
from .router import prompt_context_chars
from .vault import alternating_turns, context_parts

SYSTEM_PERSONA = (
    "You are Chat Johnson, a careful software and strategy assistant. "
    "Return useful, complete output, state uncertainty, and never claim "
    "that generated code is flawless or that a scientific simulation proves "
    "physical propulsion. Do not reveal private chain-of-thought."
)


def build_prompt_messages(
    project_scope: str,
    user_prompt: str,
    injected_context: str = "",
    workspace: Optional[str] = None,
    thread_id: Optional[int] = None,
    max_tokens: int = 2048,
    extra_system: str = "",
) -> List[Dict[str, str]]:
    """System prompt (persona, capability card, compressed memory) followed by the live window as real turns.

    Earlier turns go in as user/assistant messages rather than a text dump, so the model treats
    them as conversation instead of imitating a transcript format. ``thread_id`` pins the memory
    to one chat; background jobs must pass it because the active thread can change under them.
    """
    # Sized so the request fits every keyed endpoint's TPM ceiling at the current output budget.
    context_chars = prompt_context_chars(int(max_tokens))
    parts = context_parts(project_scope, max_characters=context_chars, thread_id=thread_id, workspace=workspace)
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
    if memory:
        system += "\n\nPROJECT MEMORY (compressed earlier history, for reference only; never imitate its format):\n" + memory
    return [{"role": "system", "content": system}, *turns]
