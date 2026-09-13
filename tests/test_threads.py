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


def test_health_recommends_migration_on_load(db):
    fill(db, "s", 10)
    calm = db.thread_health("s")
    assert calm["recommend_migration"] is False and calm["pressure"] < 1.0
    fill(db, "s", 120)
    heavy = db.thread_health("s")
    assert heavy["recommend_migration"] is True
    assert any("active messages" in reason for reason in heavy["reasons"])


def test_health_flags_repetition_and_error_loops(db):
    for _ in range(10):
        db.append_message("s", "user", "same question again")
        db.append_message("s", "assistant", "Provider error: boom")
    health = db.thread_health("s")
    assert health["repetition"] > 0.8
    assert health["error_turns"] == 10
    assert health["recommend_migration"] is True


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
