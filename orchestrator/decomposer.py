"""Task decomposition: split a goal into ordered, typed chunks the free
models each handle well (plan with a reasoning model, execute with fast ones).
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

from .quota import QuotaLedger
from .router import TASK_TYPES, generate

DECOMPOSE_PROMPT = """You are the planning unit of a code-orchestration pipeline.
Break the GOAL into the smallest ordered steps that free-tier LLMs can each
execute reliably in one call.

Respond with STRICT JSON only (no prose, no markdown fences):
{{"steps": [{{"id": 1, "title": "...", "type": "context_load|reasoning|code_patch|quick_text|test_fix",
"description": "exact instruction for the worker model", "targets": ["optional/rel/path.py"]}}]}}

Rules:
- 2-8 steps. First step may be context_load if repo understanding is needed.
- Code changes must be code_patch steps with concrete file targets.
- End with a test_fix or quick_text verification step when code changed.

GOAL:
{goal}

MEMORY:
{memory}
"""


def _extract_json(text: str) -> Dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in decomposer output")
    return json.loads(text[start : end + 1])


def decompose(goal: str, memory_block: str, ledger: QuotaLedger) -> List[Dict]:
    """Ask a reasoning-capable provider for a step plan; fall back to one step."""
    try:
        text, _ = generate(
            "reasoning",
            [
                {"role": "system", "content": "You output strict JSON task plans."},
                {"role": "user", "content": DECOMPOSE_PROMPT.format(goal=goal, memory=memory_block or "(none)")},
            ],
            ledger,
            max_tokens=2048,
            temperature=0.1,
        )
        data = _extract_json(text)
        steps = []
        for i, s in enumerate(data.get("steps", []), start=1):
            step_type = s.get("type", "code_patch")
            if step_type not in TASK_TYPES:
                step_type = "code_patch"
            steps.append(
                {
                    "id": int(s.get("id", i)),
                    "title": str(s.get("title", f"Step {i}"))[:120],
                    "type": step_type,
                    "description": str(s.get("description", "")).strip(),
                    "targets": [str(t) for t in s.get("targets", [])][:10],
                }
            )
        if steps:
            return steps[:8]
    except Exception:
        pass
    # Fallback: single code_patch step so the pipeline still runs
    return [
        {
            "id": 1,
            "title": goal[:80] or "Execute task",
            "type": "code_patch",
            "description": goal,
            "targets": [],
        }
    ]
