"""Leisure: an agent spends earned tokens on an inquiry it chooses, and the findings join its dream bank.

Exploration, not exploitation: nothing here is work. The agent names its next interest and how long
it wants to sleep, so the society wakes agents as they choose (within bounds).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence

from ..jobs import JobContext
from ..keyword_search import keyword_rank, keywords
from . import economy, store
from .cycles import CycleBudget, CycleBudgetExceeded
from .inquiries import SOURCES, inquire

LEISURE_COST = 300  # balance an agent needs before it may explore
MAX_PER_TICK = 3
MIN_SLEEP_HOURS = 0.5
MAX_SLEEP_HOURS = 24.0
DREAM_CHARS = 1_200
CHOOSE_PROMPT = (
    "You are off the clock. Choose one inquiry you are genuinely curious about, related to your focus or your recent work. "
    "Sources: {sources}. Reply with lines only: 'SOURCE: <one source>', 'QUERY: <a short search query>', 'WAKE_HOURS: <0.5-24>'.\n"
    "Your last interest: {interest}\nYour recent work: {recent}"
)
SUMMARISE_PROMPT = "Write 120–200 words of notes on this material for your own future work: what matters, one surprising thing, and how it connects to your focus. Material ({source}):\n{text}"
_LINE_RE = re.compile(r"^\s*(SOURCE|QUERY|WAKE_HOURS)\s*:\s*(.+?)\s*$", re.I | re.M)


def dream_excerpt_for(scope: str, agent_id: Optional[int], prompt: str, limit_chars: int = DREAM_CHARS) -> str:
    """The agent's own research most relevant to this prompt (keyword ranked, bounded)."""
    if not agent_id:
        return ""
    rows = store.rows("dream_bank", scope, "agent_id = ?", (int(agent_id),), order="id DESC", limit=60)
    if not rows:
        return ""
    ranked = keyword_rank(prompt, rows, key=lambda r: f"{r['query']} {r['findings']}", limit=3) or rows[:1]
    out: List[str] = []
    used = 0
    for row in ranked:
        piece = f"- [{row['source']}] {row['query']}: {row['findings']}"
        if used + len(piece) > limit_chars:
            piece = piece[: max(0, limit_chars - used)]
        if not piece:
            break
        out.append(piece)
        used += len(piece)
    return "\n".join(out)


def due_agents(scope: str, cap: int = MAX_PER_TICK) -> List[Dict[str, Any]]:
    now = time.time()
    agents = [a for a in store.agents_for(scope) if a["employment"] != "fired" and float(a.get("wake_after") or 0) <= now and int(a.get("balance") or 0) >= LEISURE_COST]
    agents.sort(key=lambda a: (float(a.get("wake_after") or 0), -int(a.get("balance") or 0)))
    return agents[:cap]


def _recent_work(scope: str, agent: Dict[str, Any]) -> str:
    seat = store.row("seats", int(agent["seat_id"])) if agent.get("seat_id") else None
    if seat:
        items = store.rows("work_items", scope, "seat_id = ?", (int(seat["id"]),), order="updated_at DESC", limit=2)
        if items:
            return "; ".join(i["title"] for i in items)
        return seat["title"]
    latest = store.rows("evaluations", scope, "agent_id = ?", (int(agent["id"]),), order="id DESC", limit=1)
    return latest[0]["prompt_key"] if latest else "none yet"


def run_leisure(ctx: JobContext, budget: CycleBudget, cycle_id: int, call_free, cap: int = MAX_PER_TICK, custom_sources: Optional[Sequence[Dict[str, Any]]] = None, call_tokens: int = 400, mode: str = "normal") -> List[Dict[str, Any]]:
    """Let the due agents explore once each; returns what each did. ``call_free`` is academy.call_free."""
    scope = ctx.project_scope
    done: List[Dict[str, Any]] = []
    sources = list(SOURCES) + [str(s.get("name")) for s in (custom_sources or []) if s.get("name")]
    for agent in due_agents(scope, cap):
        ctx.check_cancel()
        entry: Dict[str, Any] = {"agent": agent["name"]}
        try:
            choice, _ = call_free(ctx, budget, cycle_id, agent, CHOOSE_PROMPT.format(sources=", ".join(sources), interest=agent.get("interest") or "none yet", recent=_recent_work(scope, agent)), "quick_text", min(call_tokens, 150), mode=mode, role_note="Leisure: explore, do not work.", prefer_local=True)
        except CycleBudgetExceeded:
            entry["stopped"] = "budget"
            done.append(entry)
            break
        fields = {k.upper(): v for k, v in _LINE_RE.findall(choice)}
        source = fields.get("SOURCE", "").strip().lower()
        query = fields.get("QUERY", "").strip()[:120]
        try:
            hours = float(re.sub(r"[^0-9.]", "", fields.get("WAKE_HOURS", "")) or 6.0)
        except ValueError:
            hours = 6.0
        hours = max(MIN_SLEEP_HOURS, min(MAX_SLEEP_HOURS, hours))
        if source not in sources or not query:
            source = source if source in sources else sources[(int(agent["id"]) + cycle_id) % len(sources)]
            query = query or (agent.get("focus") or "creativity")
        url, text = "", ""
        status = "done"
        try:
            url, text = inquire(source, query, list(custom_sources or []))
            if not text:
                status = "empty"
        except Exception as exc:  # the source is down or blocked: log it, charge nothing more
            status = f"error: {str(exc)[:120]}"
        findings = ""
        tokens = 0
        if text:
            before = budget.tokens
            try:
                findings, _ = call_free(ctx, budget, cycle_id, agent, SUMMARISE_PROMPT.format(source=source, text=text[:4000]), "quick_text", call_tokens, mode=mode, role_note="Leisure notes for your dream bank.", prefer_local=True)
            except CycleBudgetExceeded:
                status = "budget"
            tokens = budget.tokens - before
            if findings.strip():
                store.insert("dream_bank", scope, agent_id=int(agent["id"]), timestamp=time.time(), source=source, query=query, findings=findings.strip()[:4000], tokens=tokens, tags=" ".join(dict.fromkeys(keywords(query + " " + findings)))[:300])
        spent = economy.debit(scope, int(agent["id"]), max(LEISURE_COST, tokens), f"leisure inquiry: {source} · {query}", cycle_id=cycle_id)
        store.insert("inquiries", scope, agent_id=int(agent["id"]), source=source, url=url[:500], cost_tokens=spent, status=status, cycle_id=cycle_id, created_at=time.time())
        store.update("agents", int(agent["id"]), interest=query, wake_after=time.time() + hours * 3600, mode="explore" if status == "done" else "idle")
        entry.update({"source": source, "query": query, "status": status, "spent": spent, "wake_hours": hours})
        done.append(entry)
        if status == "budget":
            break
    return done
