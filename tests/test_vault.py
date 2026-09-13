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


def test_redaction_covers_every_supported_key_shape(db):
    samples = {
        "groq": "gsk_abcdefghijklmnopqrstuvwxyz0123",
        "nvidia": "nvapi-abcdefghijklmnopqrstuvwxyz0123",
        "cerebras": "csk-abcdefghijklmnopqrstuvwxyz0123",
        "hf": "hf_abcdefghijklmnopqrstuvwxyz0123",
        "google_new": "AQ.Ab8RN6KMDVwvwdjNI4op4GmmLqSAnVMsq",
        "google": "AIzaSyAbcdefghijklmnopqrstuvwxyz0123",
    }
    for name, secret in samples.items():
        db.append_message("scope", "user", f"{name} key is {secret}")
    for row in db.recent_messages("scope"):
        for secret in samples.values():
            assert secret not in row["content"]
        assert "[REDACTED_SECRET]" in row["content"]


def test_context_block_keeps_the_newest_rows_when_the_window_is_over_budget(db):
    for index in range(30):
        db.append_message("scope", "assistant", f"row {index:02d} " + "x" * 400)
    block = db.context_block("scope", max_characters=3000)
    assert len(block) <= 3000
    assert "row 29" in block and "row 00" not in block
    kept = [line for line in block.split("\n") if line.startswith("ASSISTANT: row")]
    assert kept == sorted(kept)  # chronological order is preserved


def test_context_block_truncates_a_single_oversized_newest_row(db):
    db.append_message("scope", "assistant", "y" * 10_000)
    block = db.context_block("scope", max_characters=2000)
    assert 0 < len(block) <= 2000
    assert block.startswith("ASSISTANT: yyy") and block.endswith("[TRUNCATED]")


def test_context_parts_splits_memory_from_live_turns_in_order(db):
    db.append_message("scope", "user", "first question")
    db.append_message("scope", "assistant", "first answer", provider="groq/x")
    db.append_message("scope", "system", "Thread migrated note")
    db.append_message("scope", "user", "second question")
    parts = db.context_parts("scope")
    assert [t["role"] for t in parts["turns"]] == ["user", "assistant", "user"]
    assert parts["turns"][0]["content"] == "first question" and parts["turns"][-1]["content"] == "second question"
    assert "[NOTE] Thread migrated note" in parts["memory"]
    assert parts["empty"] is False
    assert db.context_parts("nothing-here")["empty"] is True


def test_alternating_turns_merges_neighbours_and_hands_back_a_leading_reply():
    turns = [
        {"role": "assistant", "content": "older reply"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
        {"role": "user", "content": "unanswered"},
    ]
    leading, merged = vault.alternating_turns(turns, "now")
    assert leading == "older reply"
    assert [t["role"] for t in merged] == ["user", "assistant", "user"]
    assert merged[0]["content"] == "a\n\nb" and merged[-1]["content"] == "unanswered\n\nnow"
    assert vault.alternating_turns([], "solo") == ("", [{"role": "user", "content": "solo"}])


def test_route_log_persists_and_stats_summarize_latency_and_finish(db):
    for ms, finish in ((120, ""), (300, "length"), (900, ""), (80, "")):
        db.record_route("scope", "normal_chat", "chat", "groq/gpt-oss-120b", "normal", ms, finish, "milp; key gsk_abcdefghijklmnopqrstuvwxyz123456")
    db.record_route("scope", "task_finder", "research", "failed", "normal", 1500, "", "HTTP 429")
    db.record_route("other-scope", "normal_chat", "chat", "groq/gpt-oss-120b", "normal", 5)
    stats = {row["route"]: row for row in db.route_stats("scope")}
    assert set(stats) == {"groq/gpt-oss-120b", "failed"}
    groq = stats["groq/gpt-oss-120b"]
    assert groq["sends"] == 4 and groq["truncated"] == 1 and groq["p50_ms"] == 300 and groq["p95_ms"] == 900 and groq["share"] == 0.8
    assert stats["failed"]["sends"] == 1
    rows = db.recent_routes("scope")
    assert len(rows) == 5 and "gsk_abcdefghijklmnopqrstuvwxyz123456" not in rows[-1]["reason"]
    csv = db.routes_csv("scope")
    assert csv.startswith("id,timestamp_utc,workspace,task_type,route,mode,ms,finish,reason") and csv.count("\n") == 6
    assert db.route_stats("scope", hours=0.0) == [] or all(r["sends"] >= 1 for r in db.route_stats("scope", hours=0.0))
