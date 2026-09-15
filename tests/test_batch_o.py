"""Batch O: memory that holds. A long earlier turn is clipped instead of dropped, the request is never sent twice,
Heavy synthesis keeps the conversation, and every chat can be sent to Supabase for an SQL audit."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import prompting, vault, vaultsync  # noqa: E402
from tests.test_batch_m import FakeStorage  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    vault.initialize_database()
    return "scope-o"


def test_a_long_earlier_answer_is_clipped_head_and_tail_instead_of_dropped(db):
    thread = vault.create_thread(db, "t", workspace="normal_chat")
    vault.append_message(db, "user", "write the playbook", thread_id=thread, workspace="normal_chat")
    long_answer = "### Playbook\n" + "\n".join(f"step {i:03d}: " + "x" * 60 for i in range(160)) + "\n```python\nprint('tail')\n```"
    vault.append_message(db, "assistant", long_answer, thread_id=thread, workspace="normal_chat")
    vault.append_message(db, "user", "use the code from your first response", thread_id=thread, workspace="normal_chat")
    assert len(long_answer) > 8_000
    parts = vault.context_parts(db, 8_000, thread_id=thread)
    roles = [t["role"] for t in parts["turns"]]
    assert roles == ["assistant", "user"]  # the newest turn in full, the long answer clipped, the oldest turn dropped
    clipped = parts["turns"][0]["content"]
    assert clipped.startswith("### Playbook") and clipped.endswith("print('tail')\n```") and "characters of this turn omitted" in clipped
    assert sum(len(t["content"]) for t in parts["turns"]) <= 8_000
    # The old rule still holds for the newest turn: head kept, marked, never dropped.
    assert vault.clip_turn("y" * 10_000, 2_000, newest=True).endswith(vault.TRUNCATED_MARKER)
    assert vault.clip_turn("y" * 10_000, 100) == "" and vault.clip_turn("short", 100) == "short"


def test_the_request_is_stored_once_and_sent_once(db):
    thread = vault.create_thread(db, "t", workspace="normal_chat")
    vault.append_message(db, "user", "first", thread_id=thread, workspace="normal_chat")
    vault.append_message(db, "assistant", "answer one", thread_id=thread, workspace="normal_chat")
    vault.append_message(db, "user", "second question", thread_id=thread, workspace="normal_chat")  # stored before the prompt is built
    messages = prompting.build_prompt_messages(db, "second question", thread_id=thread, recall_share=0.0)
    turns = [m for m in messages if m["role"] != "system"]
    assert [t["content"] for t in turns] == ["first", "answer one", "second question"]
    leading, merged = vault.alternating_turns([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], "b")
    assert merged == [{"role": "user", "content": "a\n\nb"}]  # only an identical trailing turn is dropped


@pytest.fixture()
def bucket(db, monkeypatch):
    monkeypatch.setenv(vaultsync.ENV_URL, "https://ref.supabase.co/")
    monkeypatch.setenv(vaultsync.ENV_KEY, "publishable-key")
    monkeypatch.delenv(vaultsync.ENV_EXPORT_TABLE, raising=False)
    fake = FakeStorage("publishable-key")
    monkeypatch.setattr(vaultsync, "requests", fake)
    vaultsync.reset_for_tests()
    yield fake
    vaultsync.reset_for_tests()


def test_export_chats_upserts_one_row_per_thread_with_the_download_payload(db, bucket):
    assert vaultsync.export_chats(db) == {"ok": True, "threads": 0, "table": "chat_exports", "note": "no chats in this scope"}
    first = vault.create_thread(db, "Main thread", workspace="normal_chat")
    vault.append_message(db, "user", "hello", thread_id=first, workspace="normal_chat")
    vault.append_message(db, "assistant", "hi there", thread_id=first, workspace="normal_chat")
    second = vault.create_thread(db, "Task", workspace="task_finder")
    vault.create_thread("other-scope", "not mine", workspace="normal_chat")
    result = vaultsync.export_chats(db)
    assert result == {"ok": True, "threads": 2, "table": "chat_exports"}
    url = "https://ref.supabase.co/rest/v1/chat_exports"
    rows = json.loads(bucket.objects[url])
    assert {r["thread_id"] for r in rows} == {first, second} and all(r["scope"] == db for r in rows)
    main = next(r for r in rows if r["thread_id"] == first)
    assert main["message_count"] == 2 and main["workspace"] == "normal_chat" and main["title"] == "Main thread"
    assert [m["content"] for m in main["payload"]["messages"]] == ["hello", "hi there"]
    status = vaultsync.status()
    assert status["last_export_threads"] == 2 and status["last_export_at"] > 0 and status["last_error"] == ""
    bucket.key = "rotated"  # the server rejects the key: reported, never raised
    failed = vaultsync.export_chats(db)
    assert not failed["ok"] and "401" in failed["note"] and "publishable-key" not in str(vaultsync.status())
