"""Deterministic mission planning for Task Finder."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.missions import MISSION_TEMPLATES, classify_mission, task_plan
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
    assert len(task_plan("write an essay", 0)) == 1
    assert len(task_plan("write an essay", 99)) == len(MISSION_TEMPLATES["research"][1])
    ids = [step["id"] for step in task_plan("write an essay", 4)]
    assert ids == [1, 2, 3, 4]
