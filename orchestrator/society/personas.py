"""Persona blocks for seats: the deterministic part of every agent prompt (zero calls)."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..prompting import build_prompt_messages
from . import store

SKIP_LEVEL_RULE = (
    "Skip-level rule: for advice or a problem your line cannot resolve you may consult one seat above your superior "
    "({above}) or one seat below your subordinates ({below}); say so as an escalation, never act for them."
)
EA_BOARD_INSTRUCTIONS = (
    "You are the Executive Assistant to the Board of {company}. The board (the operator) speaks to you directly. "
    "Filter what they send: an idea or directive becomes backlog work; a question gets an answer from the company's state "
    "you are given; chat stays chat. Reply briefly. End every reply with one line 'FILTER: idea|directive|question|chat' "
    "and, for ideas or directives, one line per work item 'BACKLOG: <title> :: <one-paragraph brief>'. "
    "Never invent progress: the cycle reports above you are the record."
)


def _seat_title(seats_by_id: Dict[int, Dict], seat_id: Optional[int]) -> str:
    seat = seats_by_id.get(int(seat_id)) if seat_id else None
    return f"{seat['title']} ({seat['key']})" if seat else "none"


def persona_block(agent: Dict, seat: Dict, company: Dict, seats_by_id: Dict[int, Dict], dream_excerpt: str = "", kpi_note: str = "") -> str:
    roles = store.load_json(seat.get("roles"), [])
    kpis = store.load_json(seat.get("kpis"), {})
    superior = seats_by_id.get(int(seat["reports_to"])) if seat.get("reports_to") else None
    above = _seat_title(seats_by_id, superior["reports_to"] if superior else None)
    subordinates = [s for s in seats_by_id.values() if s.get("reports_to") == seat["id"]]
    below_ids = [s["id"] for sub in subordinates for s in seats_by_id.values() if s.get("reports_to") == sub["id"]]
    below = ", ".join(_seat_title(seats_by_id, i) for i in below_ids[:3]) or "none"
    lines = [
        f"SEAT: {seat['title']} at {company['name']} (agent {agent['name']}, tier {agent.get('tier', 'philosopher')}).",
        "ROLES: " + "; ".join(str(r) for r in roles) + ".",
        f"REPORTS TO: {_seat_title(seats_by_id, seat.get('reports_to'))}.",
        f"COMPANY VISION: {company.get('vision', '')}",
        "CORE VALUES: " + ", ".join(str(v) for v in store.load_json(company.get("core_values"), [])) + ".",
        "IMPORTANCE RULE: work arrives rated 1–5 by the CEO and delegated by the Executive Assistant; do the highest rating first.",
        SKIP_LEVEL_RULE.format(above=above, below=below),
    ]
    if kpis:
        lines.append("KPIs THIS WEEK: " + ", ".join(f"{k} ≥ {v}" for k, v in kpis.items()) + (f" ({kpi_note})" if kpi_note else "") + ".")
    if agent.get("persona"):
        lines.append(f"PERSONA: {agent['persona']}")
    if dream_excerpt.strip():
        lines.append("FROM YOUR OWN RESEARCH (leisure notes, use when relevant):\n" + dream_excerpt.strip())
    return "\n".join(lines)


def build_agent_messages(
    scope: str, agent: Dict, seat: Dict, company: Dict, prompt: str, thread_id: Optional[int], max_tokens: int,
    seats_by_id: Optional[Dict[int, Dict]] = None, dream_excerpt: str = "",
) -> List[Dict[str, str]]:
    seats_by_id = seats_by_id or {s["id"]: s for s in store.seats_for(scope, seat.get("company_id"))}
    block = persona_block(agent, seat, company, seats_by_id, dream_excerpt=dream_excerpt)
    return build_prompt_messages(scope, prompt, workspace="society", thread_id=thread_id, max_tokens=max_tokens, extra_system=block)


def board_persona(company: Dict, facts: Sequence[str] = ()) -> str:
    block = EA_BOARD_INSTRUCTIONS.format(company=company["name"])
    if facts:
        block += "\nCOMPANY STATE:\n" + "\n".join(f"- {fact}" for fact in facts)
    return block


ACADEMY_RULES = (
    "You are an agent of the academy (Plato's Republic): Producers do foundational work on a basic allowance, Auxiliaries "
    "guard and teach on a higher one, Philosophers pass the evaluations and graduate into a company seat. Tokens are earned "
    "by production and spent in leisure; do the task exactly, state what you could not do, never pad."
)


def academy_block(agent: Dict, role_note: str = "") -> str:
    lines = [f"ACADEMY AGENT: {agent['name']} · tier {agent.get('tier', 'producer')} · focus {agent.get('focus') or 'general'} · balance {agent.get('balance', 0)}.", ACADEMY_RULES]
    if agent.get("persona"):
        lines.append(f"PERSONA: {agent['persona']}")
    if role_note:
        lines.append(role_note)
    return "\n".join(lines)


def build_academy_messages(scope: str, agent: Dict, prompt: str, thread_id: Optional[int], max_tokens: int, role_note: str = "") -> List[Dict[str, str]]:
    return build_prompt_messages(scope, prompt, workspace="society", thread_id=thread_id, max_tokens=max_tokens, extra_system=academy_block(agent, role_note))
