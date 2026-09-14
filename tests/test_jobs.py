"""Job runner: rows in the vault, secrets in memory, cooperative cancel, ask/answer, and the mission handler."""
import json
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import jobs, vault  # noqa: E402
from orchestrator import mission_runner  # noqa: E402
from orchestrator.config import session_keys  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    saved = dict(jobs._HANDLERS)
    yield
    jobs._HANDLERS.clear()
    jobs._HANDLERS.update(saved)


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def status(job_id):
    return vault.job_by_id(job_id)["status"]


def test_enqueue_claim_run_done_with_progress_and_secret_hygiene(db):
    seen = {}

    def handler(ctx):
        seen["keys"] = session_keys()
        seen["secrets"] = dict(ctx.secrets)
        ctx.progress(step=1, total=2, text="half")
        ctx.progress(step=2)
        return {"echo": ctx.payload["goal"], "ledger_is_shared": ctx.ledger is jobs.get_quota_ledger()}

    jobs.register_handler("echo", handler)
    job_id = jobs.enqueue("scope-a", "echo", {"goal": "hi", "token": "ghp_secret_should_be_redacted_123456"}, {"GEMINI_API_KEY": "AIza-test-key"}, thread_id=None)
    row = vault.job_by_id(job_id)
    assert row["status"] == "queued" and "AIza-test-key" not in row["payload"] and jobs.secrets_held(job_id)
    runner = jobs.JobRunner(max_workers=1, poll_seconds=0.05).start()
    try:
        assert wait_for(lambda: status(job_id) == "done")
    finally:
        runner.stop()
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["result"]["echo"] == "hi" and view["progress"] == {"step": 2, "total": 2, "text": "half"}
    assert seen["keys"] == {"GEMINI_API_KEY": "AIza-test-key"} and seen["secrets"] == {"GEMINI_API_KEY": "AIza-test-key"}
    assert not jobs.secrets_held(job_id) and session_keys() == {}  # the worker's context never leaks into this thread
    assert view["result"]["ledger_is_shared"] is True  # the job and a UI send with the same keys share one bucket set


def test_handler_exception_marks_the_job_failed_with_the_error(db):
    def handler(ctx):
        raise ValueError("boom")

    jobs.register_handler("bad", handler)
    job_id = jobs.enqueue("scope-a", "bad", {}, {})
    jobs.run_job(vault.claim_job("t", ("bad",)))
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["status"] == "failed" and view["result"] == {"error": "boom", "type": "ValueError"}


def test_cancel_lands_between_steps_and_cancels_a_queued_job_immediately(db):
    started = threading.Event()

    def handler(ctx):
        started.set()
        for _ in range(200):
            ctx.sleep(0.05)
        return {"finished": True}

    jobs.register_handler("slow", handler)
    job_id = jobs.enqueue("scope-a", "slow", {}, {})
    queued = jobs.enqueue("scope-a", "slow", {}, {})
    vault.request_cancel(queued)
    assert status(queued) == "cancelled"
    runner = jobs.JobRunner(max_workers=1, poll_seconds=0.05).start()
    try:
        assert started.wait(5)
        vault.request_cancel(job_id)
        assert wait_for(lambda: status(job_id) == "cancelled")
    finally:
        runner.stop()
    assert vault.job_view(vault.job_by_id(job_id))["result"]["note"].startswith("cancelled")


def test_ask_blocks_until_answered_and_times_out_otherwise(db):
    def handler(ctx):
        tone = ctx.ask("Which tone?", timeout=5)
        return {"tone": tone}

    jobs.register_handler("asker", handler)
    job_id = jobs.enqueue("scope-a", "asker", {}, {})
    runner = jobs.JobRunner(max_workers=1, poll_seconds=0.05).start()
    try:
        assert wait_for(lambda: status(job_id) == "waiting_input")
        assert vault.job_by_id(job_id)["question"] == "Which tone?"
        assert vault.answer_job(job_id, "formal") is True
        assert vault.answer_job(job_id, "again") is False  # no longer waiting
        assert wait_for(lambda: status(job_id) == "done")
    finally:
        runner.stop()
    assert vault.job_view(vault.job_by_id(job_id))["result"] == {"tone": "formal"}

    def impatient(ctx):
        return {"tone": ctx.ask("Anyone?", timeout=0.3)}

    jobs.register_handler("impatient", impatient)
    job_id = jobs.enqueue("scope-a", "impatient", {}, {})
    jobs.run_job(vault.claim_job("t", ("impatient",)))
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["status"] == "failed" and view["result"]["type"] == "JobAnswerTimeout"


def test_reap_marks_every_unfinished_row_failed_and_list_filters(db):
    jobs.register_handler("x", lambda ctx: {})
    a = jobs.enqueue("scope-a", "x", {}, {}, thread_id=7)
    b = jobs.enqueue("scope-a", "x", {}, {}, thread_id=8)
    vault.claim_job("t", ("x",))
    vault.finish_job(b, "done", {})
    c = jobs.enqueue("scope-a", "x", {}, {}, thread_id=9)
    assert vault.reap_stale_jobs("restart") == 1  # queued rows survive unless they predate the process
    assert vault.job_view(vault.job_by_id(a))["result"] == {"error": "restart"} and status(b) == "done" and status(c) == "queued"
    assert vault.reap_stale_jobs("restart", queued_before=time.time() + 1) == 1 and status(c) == "failed"
    assert [r["id"] for r in vault.list_jobs("scope-a")] == [c, b, a]
    assert [r["id"] for r in vault.list_jobs("scope-a", ("done",))] == [b]
    assert [r["id"] for r in vault.list_jobs("scope-a", thread_id=7, kind="x")] == [a]
    assert vault.list_jobs("scope-b") == []


class Decision:
    def __init__(self, finish="stop"):
        self.provider, self.model, self.reason, self.finish = "fake", "m1", "test route", finish


def test_mission_handler_writes_the_thread_and_assembles_a_writing_deliverable(db, monkeypatch):
    calls = []

    def fake_generate(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        calls.append((mode, task_type, max_tokens, paid_slot))
        if task_type == "fail_me":
            raise RuntimeError("provider down")
        return f"Section text for {messages[-1]['content'][:20]}", Decision("length" if len(calls) == 1 else "stop")

    monkeypatch.setattr(mission_runner, "generate_mode", fake_generate)
    monkeypatch.setattr(mission_runner, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    thread = vault.active_thread("scope-a", "task_finder")
    plan = [
        {"id": 1, "kind": "writing", "title": "Draft section 1", "type": "writing", "description": "Write part one"},
        {"id": 2, "kind": "writing", "title": "Draft section 2", "type": "writing", "description": "Write part two"},
        {"id": 3, "kind": "writing", "title": "Editor notes", "type": "fail_me", "description": "Review"},
    ]
    payload = {"goal": "Write a 2 page essay on tides", "plan": plan, "mode": "heavy", "max_tokens": 900, "paid_model": "o3-mini", "paid_enabled": True}
    job_id = jobs.enqueue("scope-a", mission_runner.KIND, payload, {"paid_slot_key": "sk-paid"}, thread_id=int(thread["id"]))
    jobs.run_job(vault.claim_job("t", (mission_runner.KIND,)))
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["status"] == "done", view
    result = view["result"]
    assert result["succeeded"] == 2 and result["failed"] == 1 and result["truncated"] == 1
    assert result["failures"] == [["Editor notes", "provider down"]]
    assert result["sections"] == 2 and result["deliverable_artifact"] and result["target_words"] == 800
    assert calls[0][0] == "heavy" and calls[0][2] == 900 and calls[0][3].armed and calls[0][3].api_key == "sk-paid"
    rows = vault.recent_messages("scope-a", 50, thread_id=int(thread["id"]))
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[1]["provider"] == "fake/m1" and rows[1]["task_type"] == "writing"
    assert vault.thread_by_id(int(thread["id"]))["mission"] == "Write a 2 page essay on tides"
    assert view["progress"]["step"] == 3 and view["progress"]["last_route"] == "fake/m1"
    assert vault.recent_routes("scope-a", limit=5)
    assert "sk-paid" not in json.dumps(view)
