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


def test_rolling_window_keeps_newest_200_per_scope(db):
    for index in range(205):
        db.append_message("alpha", "user", f"alpha-{index}")
    for index in range(3):
        db.append_message("beta", "user", f"beta-{index}")
    alpha = db.recent_messages("alpha")
    beta = db.recent_messages("beta")
    assert len(alpha) == 200
    assert alpha[0]["content"] == "alpha-5"
    assert alpha[-1]["content"] == "alpha-204"
    assert [row["content"] for row in beta] == ["beta-0", "beta-1", "beta-2"]


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
