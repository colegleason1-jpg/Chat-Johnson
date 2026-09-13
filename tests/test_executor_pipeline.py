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
