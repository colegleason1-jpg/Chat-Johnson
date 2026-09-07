"""Router: classifies each task chunk and selects the best free provider.

Task types map to provider strengths:
- context_load : huge input (whole repo)  -> Gemini (1M window)
- reasoning    : planning / architecture  -> NVIDIA NIM (DeepSeek-R1)
- code_patch   : small targeted diffs     -> Groq / Cerebras (fast)
- quick_text   : summaries, titles        -> Groq / Mistral
- test_fix     : feed traceback back      -> NVIDIA NIM / OpenRouter

Selection order: strength match, quota headroom, then priority score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import PROVIDERS, Settings, get_settings
from .providers import ProviderError, chat
from .quota import QuotaLedger

TASK_TYPES = ("context_load", "reasoning", "code_patch", "quick_text", "test_fix")

_KEYWORDS = {
    "context_load": ("whole repo", "codebase", "entire project", "summarize the repo", "map dependencies", "all files"),
    "reasoning": ("design", "architecture", "plan", "strategy", "why", "trade-off", "refactor approach", "breaking down"),
    "code_patch": ("patch", "diff", "edit file", "modify", "fix the function", "add method", "update class", "implement function"),
    "quick_text": ("summarize", "title", "one sentence", "tl;dr", "shorten", "translate"),
    "test_fix": ("traceback", "test failed", "assertion", "pytest", "error:", "exception", "stack trace"),
}


@dataclass
class RouteDecision:
    provider: str
    model: str
    task_type: str
    reason: str


def classify(text: str) -> str:
    """Deterministic keyword classifier — costs zero quota."""
    low = text.lower()
    scores = {t: 0 for t in TASK_TYPES}
    for task_type, words in _KEYWORDS.items():
        for w in words:
            if w in low:
                scores[task_type] += 1
    if "traceback" in low or scores["test_fix"] > 0:
        return "test_fix"
    if scores["code_patch"] and scores["reasoning"]:
        return "reasoning" if scores["reasoning"] > scores["code_patch"] else "code_patch"
    best = max(scores, key=lambda t: scores[t])
    if scores[best] == 0:
        return "code_patch" if re.search(r"def |class |import ", text) else "chat"
    return best


def candidates(task_type: str, available: List[str]) -> List[str]:
    """Providers ranked for this task type: strengths first, then priority."""
    def key(name: str) -> Tuple[int, int]:
        cfg = PROVIDERS[name]
        return (0 if task_type in cfg.strengths else 1, cfg.priority)

    return sorted(available, key=key)


def route(
    task_type: str,
    est_tokens: int,
    ledger: QuotaLedger,
    settings: Optional[Settings] = None,
) -> Optional[str]:
    """Pick the highest-ranked provider with quota headroom for this request."""
    s = settings or get_settings()
    for name in candidates(task_type, s.providers_available()):
        if PROVIDERS[name].context_window >= est_tokens and ledger.has_headroom(name, est_tokens):
            return name
    return None


def generate(
    task_type: str,
    messages: List[dict],
    ledger: QuotaLedger,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
) -> Tuple[str, RouteDecision]:
    """Route a request and fall through the candidate list on failures."""
    s = settings or get_settings()
    est_tokens = sum(len(m.get("content", "")) for m in messages) // 4 + max_tokens
    task_type = task_type if task_type in TASK_TYPES else classify(messages[-1].get("content", ""))

    tried: List[str] = []
    for name in candidates(task_type, s.providers_available()):
        if PROVIDERS[name].context_window < est_tokens or not ledger.has_headroom(name, est_tokens):
            continue
        tried.append(name)
        try:
            text, tokens = chat(name, messages, max_tokens, temperature, s)
        except ProviderError:
            continue
        ledger.record(name, tokens)
        cfg = PROVIDERS[name]
        return text, RouteDecision(name, cfg.default_model, task_type, f"strength={task_type in cfg.strengths}")
    raise ProviderError(
        "no provider available: " + (f"tried {tried}" if tried else "check API keys / quota ledger")
    )
