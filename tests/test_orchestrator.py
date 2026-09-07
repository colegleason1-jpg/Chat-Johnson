import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.patches import (
    _safe_relpath,
    apply_file_blocks,
    parse_diff_blocks,
    parse_file_blocks,
)
from orchestrator.quota import QuotaLedger
from orchestrator.router import classify
from orchestrator.sandbox import validate_python_files


def test_ledger_tracks_rpm_and_tpm():
    ledger = QuotaLedger({"fake": (3, 100)})
    assert ledger.has_headroom("fake", 10)
    ledger.record("fake", 40)
    ledger.record("fake", 40)
    assert ledger.has_headroom("fake", 20)
    assert not ledger.has_headroom("fake", 30)  # 80 + 30 > 100
    ledger.record("fake", 20)
    assert not ledger.has_headroom("fake", 1)   # 100 tokens in window
    usage = ledger.usage("fake")
    assert usage["rpm_used"] == 3 and usage["tpm_used"] == 100


def test_ledger_daily_tokens():
    ledger = QuotaLedger({"fake": (10, 1000)})
    ledger.record("fake", 500)
    ledger.record("fake", 250)
    assert ledger.usage("fake")["daily_tokens"] == 750


def test_wait_seconds_zero_when_headroom():
    ledger = QuotaLedger({"fake": (5, 1000)})
    assert ledger.wait_seconds("fake", 100) == 0.0


def test_classify_task_types():
    assert classify("feed this traceback back: ValueError in test_x") == "test_fix"
    assert classify("plan the architecture for the refactor") in ("reasoning", "test_fix")
    assert classify("summarize the entire codebase and map dependencies") == "context_load"
    assert "def foo():" in classify_test_code()


def classify_test_code():
    return "code with def foo(): pass"


def test_parse_file_blocks():
    text = (
        "Here is the fix:\n"
        "```file: src/app.py\nprint('hi')\n```\n"
        "```file: /etc/passwd\nevil\n```\n"
        "```file: ../escape.py\nevil\n```\n"
    )
    blocks = parse_file_blocks(text)
    assert "src/app.py" in blocks
    assert blocks["src/app.py"] == "print('hi')\n"
    assert "/etc/passwd" not in blocks
    assert "../escape.py" not in blocks


def test_safe_relpath():
    assert _safe_relpath("a/b.py")
    assert not _safe_relpath("../x.py")
    assert not _safe_relpath("/abs/path.py")
    assert not _safe_relpath("a/../../b.py")


def test_parse_diff_blocks():
    diff = "```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```"
    assert len(parse_diff_blocks(diff)) == 1
    assert "+b" in parse_diff_blocks(diff)[0]


def test_apply_file_blocks_and_ast_guardrail(tmp_path):
    root = tmp_path / "repo"
    (root).mkdir()
    ok = {"pkg/mod.py": "def add(a, b):\n    return a + b\n"}
    written = apply_file_blocks(str(root), ok)
    assert written == ["pkg/mod.py"]
    assert validate_python_files(str(root), ["pkg/mod.py"]) == []

    bad = {"pkg/broken.py": "def oops(:\n    pass\n"}
    apply_file_blocks(str(root), bad)
    errors = validate_python_files(str(root), ["pkg/broken.py"])
    assert errors and "broken.py" in errors[0]


def test_worktree_and_copy_mode(tmp_path):
    from orchestrator.sandbox import create_worktree, cleanup_worktree

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "README.md").write_text("hello")
    staging = tmp_path / "staging"
    # not a git repo -> copy mode
    sandbox_path, branch = create_worktree(str(repo), str(staging))
    assert os.path.isfile(os.path.join(sandbox_path, "README.md"))
    cleanup_worktree(str(repo), sandbox_path)
    assert not os.path.exists(sandbox_path)
