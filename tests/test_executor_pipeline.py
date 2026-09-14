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

    monkeypatch.setattr(executor_module, "decompose", lambda goal, memory, ledger: [
        {"id": 1, "title": "fix add", "type": "code_patch", "description": "fix the bug", "targets": ["mod.py"]}
    ])
    monkeypatch.setattr(
        executor_module, "generate",
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
    monkeypatch.setattr(executor_module, "decompose", lambda goal, memory, ledger: [
        {"id": 1, "title": "scaffold", "type": "code_patch", "description": "create the structure", "targets": []},
        {"id": 2, "title": "explain", "type": "quick_text", "description": "summarize", "targets": []},
    ])
    answers = {
        "code_patch": "### README.md\n```markdown\n# Study app\n```\n\n**src/app.py**\n```python\nprint('hi')\n```\n",
        "quick_text": "Two files were created.",
    }
    monkeypatch.setattr(
        executor_module, "generate",
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
    monkeypatch.setattr(executor_module, "decompose", lambda goal, memory, ledger: [
        {"id": 1, "title": "scaffold", "type": "code_patch", "description": "create", "targets": []},
    ])
    monkeypatch.setattr(
        executor_module, "generate",
        lambda task_type, messages, ledger, max_tokens=0, temperature=0.0: ("Sure! Here is a plan: first make folders…", RouteDecision("fake", "m", task_type, "r")),
    )
    settings = Settings(staging_root=str(tmp_path / "staging"), max_test_rounds=0, memory_path=str(tmp_path / "mem.json"))
    report = Orchestrator(settings=settings, ledger=QuotaLedger({})).run("build it", repo_path=str(repo))
    assert report["diff"].strip() == "" or "no text files differ" in report["diff"]
    step = report["steps"][0]
    assert step["status"] == "failed" and "no file blocks" in step["note"] and "answer began: Sure!" in step["note"]
