"""Deterministic mission planning for Task Finder."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.missions import (
    MAX_SECTIONS,
    MISSION_TEMPLATES,
    assemble_deliverable,
    classify_mission,
    mission_hints,
    parse_length_target,
    task_plan,
    text_measure,
    writing_sections,
)
from orchestrator.router import TASK_TYPES


def test_research_mission_gets_research_workstreams_not_code_ones():
    goal = "Help me write a research project on pink waves"
    assert classify_mission(goal) == "research"
    plan = task_plan(goal, 3)
    titles = [step["title"] for step in plan]
    assert titles == ["Frame the question", "Prior work and evidence", "Method and evidence plan"]
    assert "Repository/context analysis" not in titles and "Implementation approach" not in titles
    assert all(step["type"] in TASK_TYPES for step in plan)
    assert all("pink waves" in step["description"] for step in plan)


def test_code_mission_keeps_code_workstreams():
    plan = task_plan("Fix the bug in the router module and add unit tests", 5)
    assert plan[0]["kind"] == "code"
    assert [step["type"] for step in plan] == ["reasoning", "context_load", "code_patch", "test_fix", "quick_text"]


def test_analysis_plan_and_general_fallback():
    assert classify_mission("Analyze this dataset and forecast next quarter metrics") == "analysis"
    assert classify_mission("something with no known keywords at all") == "general"
    assert classify_mission("") == "general"
    assert len(task_plan("what should I do", 10)) == len(MISSION_TEMPLATES["general"][1])


def test_word_boundaries_and_bounds():
    # "reported" must not match "report"; "plan" inside "planet" must not match "plan"
    assert classify_mission("the planet was reported blue") == "general"
    assert len(task_plan("explain the theory", 0)) == 1
    assert len(task_plan("explain the theory", 99)) == len(MISSION_TEMPLATES["research"][1])
    ids = [step["id"] for step in task_plan("explain the theory", 4)]
    assert ids == [1, 2, 3, 4]


def test_writing_missions_produce_the_deliverable_not_meta_talk():
    goal = "Write an 1000 line essay about the history of Rome"
    assert classify_mission(goal) == "writing"
    plan = task_plan(goal, 3, max_tokens=2048)
    titles = [step["title"] for step in plan]
    assert titles == ["Brief and section plan", "Draft section 1 of 3", "Draft section 2 of 3", "Draft section 3 of 3", "Editor's notes"]
    assert plan[1]["type"] == "chat" and "Output only the section" in plan[1]["description"]
    assert all("for: Write" not in step["description"] for step in plan)
    assert "history of Rome" in plan[1]["description"]
    assert classify_mission("Help me write a research project on pink waves") == "research"  # research-shaped requests keep research steps
    assert classify_mission("write a report on Q3 sales") == "writing"


def test_length_target_and_section_sizing():
    assert parse_length_target("Write an 1000 line essay")["words"] == 10_000
    assert parse_length_target("a 2k word article")["words"] == 2_000
    assert parse_length_target("five pages") is None and parse_length_target("5 pages")["words"] == 2_000
    assert parse_length_target("no length here") is None
    assert writing_sections(10_000, 2048) == 9 and writing_sections(500, 2048) == 1
    assert writing_sections(10_000, 8192) == 3 and writing_sections(100_000, 256) == MAX_SECTIONS
    assert len(task_plan("write an essay", 99)) == MAX_SECTIONS + 2


def test_hints_assembly_and_measure():
    assert any("No topic" in hint for hint in mission_hints("Write an 1000 line essay", "writing", 9))
    assert not any("No topic" in hint for hint in mission_hints("Write an essay about coral reefs", "writing", 2))
    assert mission_hints("fix the bug", "code") == []
    text = assemble_deliverable("Write an essay about coral reefs", ["## Intro\nline one", "", "## Body\nline two\nline three"])
    assert text.startswith("# Write an essay about coral reefs\n\n## Intro") and "## Body" in text
    assert text_measure(text) == {"lines": 6, "words": 17}
