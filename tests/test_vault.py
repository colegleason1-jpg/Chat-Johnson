"""Tests for the local SQLite vault: rolling window, artifacts, redaction."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import vault


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    vault.initialize_database()
    yield vault


def test_rolling_window_texturizes_and_archives_instead_of_deleting(db):
    for index in range(205):
        db.append_message("alpha", "user", f"alpha-{index}. Decided to fix bug in core/app.py")
    for index in range(3):
        db.append_message("beta", "user", f"beta-{index}")
    alpha = db.recent_messages("alpha")
    archived = db.archived_messages("alpha")
    summaries = db.recent_summaries("alpha")
    beta = db.recent_messages("beta")
    # Active window never exceeds the cap and evicts in one coherent block.
    assert db.MESSAGE_WINDOW - db.TEXTURIZE_BATCH <= len(alpha) <= db.MESSAGE_WINDOW
    assert len(alpha) + len(archived) == 205
    assert alpha[-1]["content"].startswith("alpha-204")
    assert archived[0]["content"].startswith("alpha-0")
    # One summary covers exactly the archived block, and raw text is preserved.
    assert len(summaries) == 1
    assert summaries[0]["message_count"] == len(archived)
    assert summaries[0]["covers_from_id"] == archived[0]["id"]
    assert summaries[0]["covers_to_id"] == archived[-1]["id"]
    assert "core/app.py" in summaries[0]["content"]
    # Other scopes are untouched.
    assert [row["content"] for row in beta] == ["beta-0", "beta-1", "beta-2"]
    assert db.archived_messages("beta") == []


def test_context_block_includes_summaries_after_eviction(db):
    for index in range(201):
        db.append_message("scope", "user", f"turn {index}")
    block = db.context_block("scope")
    assert block.startswith("[TEXTURIZED SUMMARY of")
    assert "USER: turn 200" in block


def test_retexturize_summary_replaces_content(db):
    for index in range(201):
        db.append_message("scope", "user", f"turn {index}")
    summary = db.recent_summaries("scope")[0]
    db.retexturize_summary(summary["id"], "Model summary with token sk-abcdefghijklmnopqrstuvwxyz", method="model")
    refreshed = db.recent_summaries("scope")[0]
    assert refreshed["method"] == "model"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in refreshed["content"]


def test_artifact_export_and_search(db):
    artifact_id, version = db.save_artifact("scope", "router.py", "orchestrator/router.py", "def route():\n    pass\n", "python")
    filename, body = db.export_artifact(artifact_id)
    assert filename == "router.v1.py"
    assert body.startswith("def route()")
    assert [row["id"] for row in db.search_artifacts("scope", "route")] == [artifact_id]
    assert db.search_artifacts("scope", "nothing-matches") == []
    with pytest.raises(KeyError):
        db.export_artifact(999)


def test_messages_are_redacted_before_persistence(db):
    db.append_message("scope", "user", "my key is api_key=abc123def and sk-abcdefghijklmnopqrstuvwxyz")
    row = db.recent_messages("scope")[-1]
    assert "abc123def" not in row["content"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in row["content"]
    assert "[REDACTED_SECRET]" in row["content"]


def test_invalid_role_becomes_user(db):
    db.append_message("scope", "robot", "hello")
    assert db.recent_messages("scope")[-1]["role"] == "user"


def test_context_block_is_bounded(db):
    for index in range(50):
        db.append_message("scope", "assistant", "x" * 1000)
    block = db.context_block("scope", max_characters=5000)
    assert len(block) <= 5000
    assert db.context_block("empty") == "(no prior messages in this project scope)"


def test_artifacts_version_monotonically_and_keep_history(db):
    first_id, first_version = db.save_artifact("scope", "mod.py", "pkg/mod.py", "def a():\n    return 1\n", "python")
    second_id, second_version = db.save_artifact("scope", "mod.py", "pkg/mod.py", "def a():\n    return 2\n", "python")
    assert (first_version, second_version) == (1, 2)
    assert first_id != second_id
    rows = db.recent_artifacts("scope")
    assert [row["version"] for row in rows] == [2, 1]
    assert "functions=a" in rows[0]["structural_summary"]


def test_artifact_texturization_reports_invalid_python(db):
    _, _ = db.save_artifact("scope", "bad.py", "bad.py", "def oops(:\n    pass\n", "python")
    summary = db.recent_artifacts("scope")[0]["structural_summary"]
    assert "syntax=invalid" in summary


def test_artifact_body_is_redacted(db):
    db.save_artifact("scope", "cfg.py", "cfg.py", "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz'\n", "python")
    with db._open_database() as connection:
        body = connection.execute("SELECT code_body FROM artifact_store").fetchone()[0]
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in body
