"""Repository context for the chat: bounded, map first, truncation marked."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.repo_ingest import repo_prompt_context


def make_tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "app.py").write_text("print('entry')\n" * 50)
    (tmp_path / "pkg" / "util.py").write_text("def util():\n    return 1\n" * 200)
    (tmp_path / "README.md").write_text("# readme\n")
    (tmp_path / "data.bin").write_bytes(bytes(range(256)) * 10)
    return tmp_path


def test_context_is_bounded_and_keeps_the_file_map(tmp_path):
    root = make_tree(tmp_path)
    text, stats = repo_prompt_context(str(root), 2_500, "me/proj@abc1234")
    assert len(text) <= 2_500 and stats["chars"] == len(text)
    assert text.startswith("REPOSITORY CONTEXT (me/proj@abc1234; 3 files")
    assert "- app.py" in text and "- pkg/util.py" in text and "- README.md" in text
    assert "[truncated]" in text
    assert "===== FILE: app.py =====" in text  # entry point scores highest, so it comes first


def test_large_budget_includes_everything_without_truncation(tmp_path):
    root = make_tree(tmp_path)
    text, stats = repo_prompt_context(str(root), 200_000)
    assert "[truncated]" not in text and "def util()" in text and stats["file_count"] == 3
