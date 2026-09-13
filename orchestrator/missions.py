"""Deterministic, mission-aware Task Finder planning (zero provider quota).

A mission is classified by word-boundary keyword hits into writing, research, code,
analysis, plan, or general, and expanded into typed workstreams that fit that kind. No
model call is made for planning; the operator edits the plan before launch.

Writing missions produce the deliverable itself: a brief, then one drafting step per
section sized to the requested length and the output budget, then editor's notes. Every
other kind phrases its steps around the deliverable rather than around the request, so
"write an essay on X" yields the essay, not a discussion of essay writing.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

MISSION_TEMPLATES: Dict[str, Tuple[Tuple[str, ...], List[Tuple[str, str, str]]]] = {
    "writing": (
        ("write", "writing", "essay", "article", "blog", "post", "story", "speech", "letter", "chapter", "draft",
         "memo", "newsletter", "compose", "report", "whitepaper", "documentation", "readme", "guide", "tutorial"),
        [],  # built per mission by task_plan: brief, N drafting sections, editor's notes
    ),
    "research": (
        ("research", "paper", "study", "literature", "hypothesis", "experiment", "thesis", "monograph", "survey",
         "explain", "theory", "history of", "investigate", "review the evidence"),
        [
            ("Frame the question", "reasoning", "State the research question, scope, key terms, and success criteria for the deliverable: {goal}"),
            ("Prior work and evidence", "reasoning", "Summarize the most relevant prior work, theories, data, and open debates that the deliverable must build on: {goal}. Name sources and years where known; flag uncertainty explicitly."),
            ("Method and evidence plan", "reasoning", "Design the method the deliverable will follow: {goal}. Data or sources, variables, analysis steps, validity threats, and what evidence would falsify the central claim."),
            ("Outline", "quick_text", "Produce a section-by-section outline of the deliverable with a one-sentence purpose per section: {goal}"),
            ("Draft core sections", "chat", "Write the introduction, background, and method sections of the deliverable in clear prose: {goal}. Output the sections themselves, no commentary, no filler."),
            ("Critical review checklist", "quick_text", "Write a critical review checklist (gaps, weak claims, missing citations, next steps) for the drafted deliverable: {goal}"),
        ],
    ),
    "code": (
        ("code", "implement", "bug", "fix", "refactor", "function", "class", "api", "endpoint", "unit test", "tests",
         "repository", "repo", "module", "script", "deploy", "pipeline", "library", "package", "traceback", "compile", "streamlit", "python", "javascript"),
        [
            ("Scope and constraints", "reasoning", "Identify acceptance criteria, risks, and a minimal execution boundary for: {goal}"),
            ("Repository/context analysis", "context_load", "Map the relevant files, interfaces, dependencies, and existing tests for: {goal}"),
            ("Implementation approach", "code_patch", "Propose a concrete implementation for: {goal}. Emit complete file blocks only if files are supplied."),
            ("Verification plan", "test_fix", "Define tests and failure checks that validate: {goal}. Do not weaken existing tests."),
            ("Operator handoff", "quick_text", "Write a concise review checklist and handoff summary for: {goal}"),
        ],
    ),
    "analysis": (
        ("analyze", "analysis", "data", "dataset", "metrics", "forecast", "statistics", "compare", "evaluate",
         "benchmark", "spreadsheet", "csv", "trend", "model the", "estimate"),
        [
            ("Question and metrics", "reasoning", "Define the exact question, the metrics that answer it, and the decision they inform for: {goal}"),
            ("Data and assumptions", "reasoning", "List the data needed, where it comes from, its limits, and every assumption for: {goal}"),
            ("Analysis steps", "reasoning", "Lay out the analysis steps in order with the check that validates each for: {goal}"),
            ("Findings draft", "chat", "Write the findings section of the deliverable with number placeholders clearly marked: {goal}. Output the section itself."),
            ("Caveats and next checks", "quick_text", "List caveats, sensitivity checks, and the next three actions for: {goal}"),
        ],
    ),
    "plan": (
        ("plan", "strategy", "roadmap", "business", "launch", "marketing", "budget", "schedule", "organize",
         "proposal", "pitch", "okr", "milestone", "timeline", "campaign"),
        [
            ("Objective and constraints", "reasoning", "State the objective, hard constraints, and success measures for: {goal}"),
            ("Options and trade-offs", "reasoning", "Compare two or three viable approaches with trade-offs and risks for: {goal}"),
            ("Phased plan", "chat", "Write the phased plan itself with owners, deliverables, and time boxes: {goal}. Output the plan, not advice about planning."),
            ("Risks and mitigations", "quick_text", "List the top risks with a mitigation and an early warning sign each for: {goal}"),
            ("First week actions", "quick_text", "List the concrete actions for the first week for: {goal}"),
        ],
    ),
    "general": (
        (),
        [
            ("Clarify the objective", "reasoning", "Restate the objective precisely, list unknowns, and state assumptions for: {goal}"),
            ("Key considerations", "reasoning", "Identify the factors that decide the outcome and how they interact for: {goal}"),
            ("Recommended approach", "chat", "Give a concrete recommended approach with reasoning for: {goal}"),
            ("Next actions", "quick_text", "List the next concrete actions and what would change the recommendation for: {goal}"),
        ],
    ),
}

# On a tie, an explicitly research-shaped request keeps research workstreams; everything else that
# names a thing to write is a writing mission.
_TIE_ORDER = ("research", "writing", "code", "analysis", "plan", "general")

MAX_SECTIONS = 12
DEFAULT_TARGET_WORDS = 800
WORDS_PER_LINE = 10
WORDS_PER_PAGE = 400
WORDS_PER_PARAGRAPH = 90
CHARS_PER_WORD = 5.5

_LENGTH_RE = re.compile(
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<k>k)?\s*[- ]?(?P<unit>lines?|words?|pages?|paragraphs?|characters?|chars?)\b",
    re.IGNORECASE,
)


def classify_mission(goal: str) -> str:
    """Deterministic, zero-quota mission classification by keyword hits (word-boundary)."""
    lowered = goal.lower()
    scores: Dict[str, int] = {}
    for kind, (keywords, _) in MISSION_TEMPLATES.items():
        scores[kind] = sum(1 for word in keywords if re.search(r"\b" + re.escape(word) + r"\b", lowered))
    best_score = max(scores.values())
    if best_score == 0:
        return "general"
    for kind in _TIE_ORDER:
        if scores.get(kind, 0) == best_score:
            return kind
    return "general"


def parse_length_target(goal: str) -> Optional[Dict[str, Any]]:
    """'1000 line essay', '2k words', '5 pages' → {'amount', 'unit', 'words'}; None when no length is named."""
    match = _LENGTH_RE.search(goal or "")
    if not match:
        return None
    amount = float(match.group("amount").replace(",", ""))
    if match.group("k"):
        amount *= 1000
    unit = match.group("unit").lower().rstrip("s")
    if unit == "char":
        unit = "character"
    factor = {"line": WORDS_PER_LINE, "word": 1, "page": WORDS_PER_PAGE, "paragraph": WORDS_PER_PARAGRAPH, "character": 1 / CHARS_PER_WORD}[unit]
    return {"amount": int(amount), "unit": unit, "words": max(50, int(round(amount * factor)))}


def words_per_step(max_tokens: int) -> int:
    """Substantive words one drafting call can hold at ~4 chars/token and ~5.5 chars/word, with headroom."""
    return max(150, int(int(max_tokens) * 0.55))


def writing_sections(target_words: int, max_tokens: int) -> int:
    return max(1, min(MAX_SECTIONS, math.ceil(int(target_words) / words_per_step(max_tokens))))


def mission_hints(goal: str, kind: str, sections: int = 1) -> List[str]:
    """Light, deterministic warnings shown as captions; they never block a launch."""
    hints: List[str] = []
    if kind == "writing":
        stripped = re.sub(r"\b(write|writing|compose|draft|an?|the|me|please|essay|article|blog|post|story|speech|letter|chapter|memo|"
                          r"newsletter|report|whitepaper|guide|tutorial|readme|documentation|of|about|on|for|long|short|with)\b", " ", goal.lower())
        stripped = _LENGTH_RE.sub(" ", stripped)
        if len(re.sub(r"[^a-z0-9]", "", stripped)) < 4:
            hints.append("No topic is named; the brief step will choose one. Add 'about …' to the mission to steer it.")
        if sections >= 8:
            hints.append(f"Long deliverable: {sections} drafting calls; raise the output token budget to need fewer.")
    return hints


def _writing_plan(goal: str, sections: int, max_tokens: int) -> List[Dict[str, Any]]:
    target = parse_length_target(goal)
    total_words = target["words"] if target else DEFAULT_TARGET_WORDS
    per_section_words = max(80, int(round(total_words / sections)))
    per_section_lines = max(8, int(round(per_section_words / WORDS_PER_LINE)))
    clean = goal.strip()
    steps: List[Tuple[str, str, str]] = [(
        "Brief and section plan", "reasoning",
        f"Set the brief for the deliverable: {clean}. State the topic, audience, tone, and thesis, then a numbered plan of "
        f"{sections} section(s) with a one-line purpose and a length budget of about {per_section_lines} lines "
        f"({per_section_words} words) each. Output the brief only; do not write the sections yet.",
    )]
    for index in range(1, sections + 1):
        steps.append((
            f"Draft section {index} of {sections}", "chat",
            f"Write section {index} of {sections} of the deliverable: {clean}. Follow the section plan from the brief, "
            f"continue from the previous section without repeating it, and target about {per_section_lines} lines "
            f"({per_section_words} words) of substantive prose. Output only the section, starting with its heading.",
        ))
    steps.append((
        "Editor's notes", "quick_text",
        f"Review every drafted section of the deliverable: {clean}. List continuity breaks, repetition, gaps against the "
        "brief, and the exact fix for each. Do not rewrite the sections.",
    ))
    return [
        {"id": index + 1, "title": title, "type": task_type, "description": description, "status": "queued", "kind": "writing"}
        for index, (title, task_type, description) in enumerate(steps)
    ]


def task_plan(goal: str, count: int, max_tokens: int = 2048) -> List[Dict[str, Any]]:
    """Mission-aware, deterministic task graph (no provider quota spent on planning).

    ``count`` is the number of workstreams for template kinds and the number of drafting
    sections for writing missions (brief and editor's notes are added around them).
    """
    kind = classify_mission(goal)
    if kind == "writing":
        return _writing_plan(goal, max(1, min(MAX_SECTIONS, int(count))), max_tokens)
    templates = MISSION_TEMPLATES[kind][1]
    bounded = max(1, min(int(count), len(templates)))
    return [
        {
            "id": index + 1,
            "title": title,
            "type": task_type,
            "description": description.format(goal=goal.strip()),
            "status": "queued",
            "kind": kind,
        }
        for index, (title, task_type, description) in enumerate(templates[:bounded])
    ]


def deliverable_slug(goal: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:40] or "deliverable"


def assemble_deliverable(goal: str, sections: Sequence[str]) -> str:
    """Join drafted sections in order under the mission title. Deterministic, zero calls."""
    body = "\n\n".join(section.strip() for section in sections if section and section.strip())
    return f"# {goal.strip()}\n\n{body}\n"


def text_measure(text: str) -> Dict[str, int]:
    lines = [line for line in text.splitlines() if line.strip()]
    return {"lines": len(lines), "words": len(text.split())}
