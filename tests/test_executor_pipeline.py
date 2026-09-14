"""The repository pipeline must run end to end with a mocked model (it used to crash on an import)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import executor as executor_module
from orchestrator.config import Settings
from orchestrator.executor import Orchestrator
from orchestrator.quota import QuotaLedger
from orchestrator.router import RouteDecision


def test_pipeline_runs_in_copy_mode_and_reports_a_real_diff(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "mod.py").write_text("def add(a, b):\n    return a - b\n")
    staging = tmp_path / "staging"

    monkeypatch.setattr(executor_module, "decompose", lambda goal, memory, ledger, generate_fn=None: [
        {"id": 1, "title": "fix add", "type": "code_patch", "description": "fix the bug", "targets": ["mod.py"]}
    ])
    monkeypatch.setattr(
        executor_module, "pipeline_generate",
        lambda task_type, messages, ledger, max_tokens=0, temperature=0.0: (
            "```file: mod.py\ndef add(a, b):\n    return a + b\n```",
            RouteDecision("fake", "m", task_type, "r"),
        ),
    )
    settings = Settings(staging_root=str(staging), max_test_rounds=0, memory_path=str(tmp_path / "mem.json"))
    report = Orchestrator(settings=settings, ledger=QuotaLedger({})).run("fix add", repo_path=str(repo))
    assert report["branch"] == "(copy-mode)"
    assert "+    return a + b" in report["diff"] and "-    return a - b" in report["diff"]
    assert (repo / "mod.py").read_text().endswith("return a - b\n")  # source tree untouched


def test_pipeline_scaffolds_an_empty_tree_from_heading_style_answers_and_reports_steps(tmp_path, monkeypatch):
    repo = tmp_path / "blank"
    repo.mkdir()
    staging = tmp_path / "staging"
    monkeypatch.setattr(executor_module, "decompose", lambda goal, memory, ledger, generate_fn=None: [
        {"id": 1, "title": "scaffold", "type": "code_patch", "description": "create the structure", "targets": []},
        {"id": 2, "title": "explain", "type": "quick_text", "description": "summarize", "targets": []},
    ])
    answers = {
        "code_patch": "### README.md\n```markdown\n# Study app\n```\n\n**src/app.py**\n```python\nprint('hi')\n```\n",
        "quick_text": "Two files were created.",
    }
    monkeypatch.setattr(
        executor_module, "pipeline_generate",
        lambda task_type, messages, ledger, max_tokens=0, temperature=0.0: (answers[task_type], RouteDecision("fake", "m", task_type, "r")),
    )
    settings = Settings(staging_root=str(staging), max_test_rounds=0, memory_path=str(tmp_path / "mem.json"))
    report = Orchestrator(settings=settings, ledger=QuotaLedger({})).run("build it", repo_path=str(repo))
    assert "+# Study app" in report["diff"] and "+print('hi')" in report["diff"]
    assert [s["status"] for s in report["steps"]] == ["done", "done"] and report["steps"][0]["type"] == "code_patch"
    assert os.listdir(str(repo)) == []  # the source tree is untouched; files live in the sandbox


def test_pipeline_reports_why_nothing_was_written(tmp_path, monkeypatch):
    repo = tmp_path / "blank2"
    repo.mkdir()
    monkeypatch.setattr(executor_module, "decompose", lambda goal, memory, ledger, generate_fn=None: [
        {"id": 1, "title": "scaffold", "type": "code_patch", "description": "create", "targets": []},
    ])
    monkeypatch.setattr(
        executor_module, "pipeline_generate",
        lambda task_type, messages, ledger, max_tokens=0, temperature=0.0: ("Sure! Here is a plan: first make folders…", RouteDecision("fake", "m", task_type, "r")),
    )
    settings = Settings(staging_root=str(tmp_path / "staging"), max_test_rounds=0, memory_path=str(tmp_path / "mem.json"))
    report = Orchestrator(settings=settings, ledger=QuotaLedger({})).run("build it", repo_path=str(repo))
    assert report["diff"].strip() == "" or "no text files differ" in report["diff"]
    step = report["steps"][0]
    assert step["status"] == "failed" and "no file blocks" in step["note"] and "answer began: Sure!" in step["note"]


def test_empty_repository_gets_a_fixed_scaffold_plan_without_a_planning_call(tmp_path, monkeypatch):
    repo = tmp_path / "blank3"
    repo.mkdir()
    called = {"decompose": 0, "calls": []}

    def no_decompose(goal, memory, ledger, generate_fn=None):
        called["decompose"] += 1
        return []

    monkeypatch.setattr(executor_module, "decompose", no_decompose)

    def fake_generate(task_type, messages, ledger, max_tokens=0, temperature=0.0):
        called["calls"].append((task_type, max_tokens))
        if task_type == "code_patch":
            return "```file: README.md\n# app\n```\n```file: src/main.py\nprint(1)\n```", RouteDecision("fake", "m", task_type, "r")
        return "Run python src/main.py", RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setattr(executor_module, "pipeline_generate", fake_generate)
    settings = Settings(staging_root=str(tmp_path / "staging"), max_test_rounds=0, memory_path=str(tmp_path / "mem.json"), max_output_tokens=1234)
    report = Orchestrator(settings=settings, ledger=QuotaLedger({})).run("study app", repo_path=str(repo))
    assert called["decompose"] == 0
    step_calls = [(t, m) for t, m in called["calls"] if m != 256]  # 256-token calls are the memory summarizer
    assert step_calls == [("code_patch", 1234), ("quick_text", 1234)]
    assert report["failed_steps"] == 0 and "+print(1)" in report["diff"]


def test_failed_steps_are_counted_and_named(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(executor_module, "decompose", lambda goal, memory, ledger, generate_fn=None: [
        {"id": 1, "title": "patch", "type": "code_patch", "description": "d", "targets": ["a.py"]},
    ])

    def boom(task_type, messages, ledger, max_tokens=0, temperature=0.0):
        raise RuntimeError("no headroom anywhere")

    monkeypatch.setattr(executor_module, "pipeline_generate", boom)
    settings = Settings(staging_root=str(tmp_path / "staging"), max_test_rounds=0, memory_path=str(tmp_path / "mem.json"))
    report = Orchestrator(settings=settings, ledger=QuotaLedger({})).run("do it", repo_path=str(repo))
    assert report["failed_steps"] == 1 and report["steps"][0]["provider"] == "none" and "no headroom" in report["steps"][0]["note"]
