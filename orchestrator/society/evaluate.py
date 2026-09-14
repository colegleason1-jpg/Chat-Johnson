"""Deterministic checks that pair with every model-graded review (a grade alone can be gamed)."""
from __future__ import annotations

import re
from typing import Dict

from ..keyword_search import keywords

PLACEHOLDER_RE = re.compile(r"\b(lorem ipsum|TODO|TBD|\[insert|placeholder text|as an ai language model)\b", re.I)
STOP = {"this", "that", "with", "from", "into", "each", "your", "their", "will", "what", "when", "where", "which", "about", "under", "over", "every", "write", "draft", "make"}
RUBRICS: Dict[str, str] = {
    "context_awareness": "Given the brief and the company state, does the answer use the facts it was given and avoid inventing others?",
    "planning": "Does the answer lay out ordered, concrete steps with what each needs and what it produces?",
    "synthesis": "Does the answer combine the inputs into one coherent result rather than restating them?",
}


def deterministic_check(brief: str, text: str, min_words: int = 80) -> Dict[str, object]:
    """Length, brief coverage, and placeholder scan; ``passed`` when the score clears 0.6 with no placeholders."""
    words = len(text.split())
    terms = [w for w in dict.fromkeys(keywords(brief)) if len(w) >= 4 and w not in STOP][:12]
    lowered = text.lower()
    covered = sum(1 for w in terms if w in lowered)
    coverage = covered / len(terms) if terms else 1.0
    placeholders = bool(PLACEHOLDER_RE.search(text))
    score = 0.5 * min(1.0, words / max(1, min_words)) + 0.5 * coverage - (0.3 if placeholders else 0.0)
    return {"words": words, "coverage": round(coverage, 2), "placeholders": placeholders, "score": round(max(0.0, score), 2), "passed": score >= 0.6 and not placeholders}
