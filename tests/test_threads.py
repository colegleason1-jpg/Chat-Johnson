"""Threads, health sweep, vision digest, and optimized migration."""
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


def fill(db, scope, count, thread_id=None, text="turn {i}"):
    for index in range(count):
        db.append_message(scope, "user" if index % 2 == 0 else "assistant", text.format(i=index), thread_id=thread_id)


def test_first_use_creates_main_thread_and_scopes_are_independent(db):
    a = db.active_thread("alpha")
    b = db.active_thread("beta")
    assert a["title"] == "Main thread" and b["title"] == "Main thread"
    assert a["id"] != b["id"]
    fill(db, "alpha", 3)
    assert len(db.recent_messages("alpha")) == 3
    assert db.recent_messages("beta") == []


def test_threads_isolate_messages_and_windows(db):
    main = db.active_thread("s")["id"]
    fill(db, "s", 5)
    second = db.create_thread("s", "Design chat")
    assert db.active_thread("s")["id"] == second  # newest active thread is current
    fill(db, "s", 2)
    assert [r["content"] for r in db.recent_messages("s")] == ["turn 0", "turn 1"]
    assert len(db.recent_messages("s", thread_id=main)) == 5
    db.switch_thread(main)
    assert db.active_thread("s")["id"] == main
    # the window and texturization apply per thread
    fill(db, "s", 201, thread_id=main)
    assert len(db.recent_messages("s", thread_id=main)) <= db.MESSAGE_WINDOW
    assert len(db.recent_summaries("s", thread_id=main)) == 1
    assert db.recent_summaries("s", thread_id=second) == []


def test_clear_thread_archives_and_keeps_keys_out_of_scope(db):
    fill(db, "s", 6)
    thread_id = db.active_thread("s")["id"]
    moved = db.clear_thread(thread_id)
    assert moved == 6
    assert db.recent_messages("s") == []
    assert len(db.archived_messages("s")) == 6
    assert db.active_thread("s")["id"] == thread_id  # same thread, now empty


def test_health_recommends_migration_on_load_only_after_texturization(db):
    fill(db, "s", 10)
    calm = db.thread_health("s")
    assert calm["recommend_migration"] is False and calm["pressure"] < 1.0
    fill(db, "s", 200)  # 210 total: past the 200 window, so a texturized block exists, but under the 300 limit
    mid = db.thread_health("s")
    assert mid["summaries"] >= 1 and mid["recommend_migration"] is False
    fill(db, "s", 100)  # 310 total
    heavy = db.thread_health("s")
    assert heavy["recommend_migration"] is True
    assert any("messages on this thread" in reason for reason in heavy["reasons"])
    assert heavy["pressure"] >= 1.0  # the gauge and the recommendation are the same predicate


def test_error_loops_and_repetition_are_advisory_not_migration_triggers(db):
    for _ in range(10):
        db.append_message("s", "user", "same question again")
        db.append_message("s", "assistant", "Provider error: boom")
    health = db.thread_health("s")
    assert health["repetition"] > 0.8
    assert health["error_turns"] == 10
    assert health["recommend_migration"] is False  # migrating would clear the window and re-arm the trigger
    assert any("provider errors" in note for note in health["advisories"])
    assert any("repeated prompts" in note for note in health["advisories"])


def test_migration_refuses_threads_with_nothing_to_compress(db):
    db.append_message("s", "user", "hi")
    with pytest.raises(ValueError):
        db.migrate_thread("s")


def test_selecting_a_migrated_thread_keeps_it_migrated(db):
    fill(db, "s", 30)
    result = db.migrate_thread("s")
    old = result["old_thread_id"]
    db.switch_thread(old)
    assert db.active_thread("s")["id"] == old
    assert db.thread_by_id(old)["status"] == "migrated"


def test_digest_regex_matches_real_decision_sentences(db):
    db.append_message("s", "user", "We decided to cap Groq at 8000 tpm and we require tests for every change.")
    db.append_message("s", "assistant", "Agreed. The policy is approved.")
    fill(db, "s", 4)
    digest = db.build_vision_digest("s")
    assert "## Decisions and constraints" in digest
    assert "We decided to cap Groq" in digest


def test_context_budgets_keep_summaries_after_migration(db):
    fill(db, "s", 205, text="turn {i}: we decided to keep orchestrator/app.py stable")
    db.migrate_thread("s")
    fill(db, "s", 201, text="new turn {i}")  # force a texturized block on the successor
    context = db.context_block("s")
    assert "[THREAD VISION DIGEST" in context
    assert "[TEXTURIZED SUMMARY" in context


def test_vision_digest_keeps_decisions_facts_open_items_and_artifacts(db):
    db.append_message("s", "user", "Build the router. We decided to cap Groq at 8000 tpm.\nOpen question: which Gemini id?")
    db.append_message("s", "assistant", "Edit orchestrator/router.py and keep 2 rpm for Gemini.\nTODO: add tests")
    db.save_artifact("s", "router.py", "orchestrator/router.py", "x = 1\n", "python")
    digest = db.build_vision_digest("s")
    assert "## Vision" in digest and "Build the router" in digest
    assert "8000 tpm" in digest
    assert "orchestrator/router.py" in digest
    assert "which Gemini id" in digest or "TODO: add tests" in digest
    assert "router.py v1" in digest
    assert "sk-" not in digest


def test_migration_creates_successor_with_locked_digest_and_keeps_history(db):
    fill(db, "s", 30, text="turn {i}: we must keep app.py stable")
    old = db.active_thread("s")["id"]
    result = db.migrate_thread("s", refine=lambda text: "REFINED " + text[:500])
    assert result["old_thread_id"] == old
    new = result["new_thread_id"]
    assert result["method"] == "model+extractive"
    assert db.active_thread("s")["id"] == new
    assert db.thread_by_id(old)["status"] == "migrated"
    successor = db.thread_by_id(new)
    assert successor["parent_thread_id"] == old and successor["generation"] == 2
    assert successor["digest_artifact_id"] == result["digest_artifact_id"]
    # raw history of the old thread is intact, the new thread starts from the digest
    assert len(db.recent_messages("s", thread_id=old)) == 30
    new_rows = db.recent_messages("s", thread_id=new)
    assert len(new_rows) == 1 and new_rows[0]["role"] == "system"
    context = db.context_block("s")
    assert context.startswith("[THREAD VISION DIGEST")
    assert "REFINED" in context
    # the digest is a normal locked artifact, exportable like any other
    filename, body = db.export_artifact(result["digest_artifact_id"])
    assert filename.startswith(f"thread-{old}-digest") and "app.py" in body


def test_migration_survives_a_failing_refiner(db):
    fill(db, "s", 4)
    result = db.migrate_thread("s", refine=lambda text: (_ for _ in ()).throw(RuntimeError("no key")))
    assert "extractive" in result["method"]
    assert db.thread_by_id(result["new_thread_id"])["digest_artifact_id"] is not None


def test_legacy_vault_rows_are_backfilled_into_a_thread(db):
    # simulate a pre-thread database: rows with NULL thread_id
    with db._open_database() as connection:
        connection.execute(
            "INSERT INTO message_history (role, content, timestamp, token_count, project_scope) VALUES ('user', 'old', 1.0, 1, 'legacy')"
        )
        connection.commit()
    db.initialize_database()
    thread = db.active_thread("legacy")
    rows = db.recent_messages("legacy")
    assert len(rows) == 1 and rows[0]["thread_id"] == thread["id"]


def test_delete_thread_removes_history_archive_and_summaries_only_for_that_thread(db):
    scope = "d"
    first = db.active_thread(scope)["id"]
    fill(db, scope, 6)
    db.clear_thread(first)  # six rows now live in the archive
    fill(db, scope, 4)
    second = db.create_thread(scope, "keep me")
    fill(db, scope, 3)
    db.switch_thread(first)
    assert db.active_thread(scope)["id"] == first
    counts = db.delete_thread(first)
    assert counts == {"message_history": 4, "message_archive": 6, "summaries": 0}
    assert db.thread_by_id(first) is None
    assert db.archived_messages(scope, 100, thread_id=first) == []
    assert db.active_thread(scope)["id"] == second
    assert [r["content"] for r in db.recent_messages(scope)] == ["turn 0", "turn 1", "turn 2"]
    with pytest.raises(ValueError):
        db.delete_thread(first)


def test_deleting_the_only_thread_yields_a_fresh_empty_one(db):
    scope = "e"
    only = db.active_thread(scope)["id"]
    fill(db, scope, 2)
    db.delete_thread(only)
    fresh = db.active_thread(scope)
    assert fresh["id"] != only
    assert db.recent_messages(scope) == []


def test_deleting_a_migrated_parent_keeps_the_successor_and_its_digest(db, monkeypatch):
    scope = "f"
    parent = db.active_thread(scope)["id"]
    fill(db, scope, 8, text="we decided the API must stay free-tier only; step {i}")
    result = db.migrate_thread(scope, workspace="normal_chat")
    successor = db.thread_by_id(result["new_thread_id"])
    assert int(successor["parent_thread_id"]) == parent
    db.delete_thread(parent)
    successor = db.thread_by_id(result["new_thread_id"])
    assert successor is not None and successor["parent_thread_id"] is None
    assert db.artifact_by_id(result["digest_artifact_id"]) is not None


def test_messages_record_their_task_type(db):
    db.append_message("t", "user", "find sources", task_type="research")
    db.append_message("t", "assistant", "here they are", provider="groq/x", task_type="research")
    db.append_message("t", "user", "thanks")
    rows = db.recent_messages("t")
    assert [r["task_type"] for r in rows] == ["research", "research", ""]


def test_mission_is_pinned_to_the_chat_and_survives_migration_but_not_clear(db):
    scope = "m"
    thread_id = db.active_thread(scope, "task_finder")["id"]
    db.set_thread_mission(thread_id, "  Ship the free-tier router  ")
    assert db.thread_by_id(thread_id)["mission"] == "Ship the free-tier router"
    fill(db, scope, 8, thread_id=thread_id, text="we decided step {i} must stay free-tier")
    result = db.migrate_thread(scope, workspace="task_finder")
    successor = db.active_thread(scope, "task_finder")
    assert successor["id"] == result["new_thread_id"]
    assert successor["mission"] == "Ship the free-tier router"
    db.clear_thread(int(successor["id"]))
    assert db.thread_by_id(int(successor["id"]))["mission"] == ""


def test_locked_artifacts_survive_delete_thread(db):
    scope = "art"
    thread_id = db.active_thread(scope)["id"]
    message_id = db.append_message(scope, "assistant", "```python\nprint(1)\n```")
    artifact_id, _ = db.save_artifact(scope, "keep.py", "keep.py", "print(1)\n", "python", source_message_id=message_id)
    db.delete_thread(thread_id)
    row = db.artifact_by_id(artifact_id)
    assert row is not None
    assert row["source_message_id"] is None  # ON DELETE SET NULL keeps the artifact, drops the dangling link
    assert row["code_body"] == "print(1)\n"


def test_task_type_survives_archiving_by_clear_and_by_window_eviction(db):
    scope = "tt"
    thread_id = db.active_thread(scope)["id"]
    db.append_message(scope, "user", "first", task_type="research")
    db.clear_thread(thread_id)
    assert [r["task_type"] for r in db.archived_messages(scope, 10, thread_id=thread_id)] == ["research"]
    for index in range(db.MESSAGE_WINDOW + 1):
        db.append_message(scope, "user", f"m{index}", task_type="code_patch")
    evicted = db.archived_messages(scope, 5000, thread_id=thread_id)
    assert len(evicted) > 1 and all(r["task_type"] == "code_patch" for r in evicted[1:])
