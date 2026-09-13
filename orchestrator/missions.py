"""Deterministic, mission-aware Task Finder planning (zero provider quota).

A mission is classified by word-boundary keyword hits into research, code,
analysis, plan, or general, and expanded into typed workstreams that fit that
kind. No model call is made for planning; the operator edits the plan before
launch.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

MISSION_TEMPLATES: Dict[str, Tuple[Tuple[str, ...], List[Tuple[str, str, str]]]] = {
    "research": (
        ("research", "paper", "study", "literature", "hypothesis", "experiment", "thesis", "essay", "report",
         "write", "writing", "article", "monograph", "survey", "explain", "theory", "history of", "investigate"),
        [
            ("Frame the question", "reasoning", "State the research question, scope, key terms, and success criteria for: {goal}"),
            ("Prior work and evidence", "reasoning", "Summarize the most relevant prior work, theories, data, and open debates for: {goal}. Name sources and years where known; flag uncertainty explicitly."),
            ("Method and evidence plan", "reasoning", "Design the method for: {goal}. Data or sources, variables, analysis steps, validity threats, and what evidence would falsify the central claim."),
            ("Outline", "quick_text", "Produce a section-by-section outline with a one-sentence purpose per section for: {goal}"),
            ("Draft core sections", "chat", "Draft the introduction, background, and method sections in clear prose for: {goal}. No filler."),
            ("Critical review checklist", "quick_text", "Write a critical review checklist (gaps, weak claims, missing citations, next steps) for: {goal}"),
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
            ("Findings draft", "chat", "Draft the findings section with numbers placeholders clearly marked for: {goal}"),
            ("Caveats and next checks", "quick_text", "List caveats, sensitivity checks, and the next three actions for: {goal}"),
        ],
    ),
    "plan": (
        ("plan", "strategy", "roadmap", "business", "launch", "marketing", "budget", "schedule", "organize",
         "proposal", "pitch", "okr", "milestone", "timeline", "campaign"),
        [
            ("Objective and constraints", "reasoning", "State the objective, hard constraints, and success measures for: {goal}"),
            ("Options and trade-offs", "reasoning", "Compare two or three viable approaches with trade-offs and risks for: {goal}"),
            ("Phased plan", "chat", "Write a phased plan with owners, deliverables, and time boxes for: {goal}"),
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


def classify_mission(goal: str) -> str:
    """Deterministic, zero-quota mission classification by keyword hits (word-boundary)."""
    lowered = goal.lower()
    scores: Dict[str, int] = {}
    for kind, (keywords, _) in MISSION_TEMPLATES.items():
        scores[kind] = sum(1 for word in keywords if re.search(r"\b" + re.escape(word) + r"\b", lowered))
    best = max(scores, key=lambda kind: scores[kind])
    return best if scores[best] > 0 else "general"


def task_plan(goal: str, count: int) -> List[Dict[str, Any]]:
    """Mission-aware, deterministic task graph (no provider quota spent on planning)."""
    kind = classify_mission(goal)
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
