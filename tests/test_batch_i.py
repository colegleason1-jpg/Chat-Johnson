"""Batch I: outcome logging with the counterfactual and pink-wave state; FTS5 long-distance recall with decay and superseding."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import pinkwave, vault  # noqa: E402
from orchestrator.router import CORTEX_ENDPOINTS, RouteDecision, select_milp_endpoint  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    pinkwave.deactivate()
    yield "scope-i"
    pinkwave.deactivate()


# ----------------------------------------------------------------------------- outcomes

def test_route_log_keeps_the_counterfactual_and_takes_a_verdict(db):
    chaos = {"gain": 0.25, "profile": "pink", "step": 7, "jitter": 0.0123}
    row_id = vault.record_route(db, "normal_chat", "chat", "groq/gpt-oss-120b", "normal", 300, "", "milp", runner_up="google_ai_studio", chaos=chaos, message_id=41)
    vault.record_route(db, "normal_chat", "chat", "google_ai_studio/gemini", "normal", 900, "length", "milp", runner_up="groq", message_id=42)
    vault.record_route(db, "task_finder", "research", "failed", "normal", 1500, "", "HTTP 429")
    assert row_id and vault.set_route_outcome(db, 41, "up") == 1 and vault.set_route_outcome(db, 42, "locked") == 1
    assert vault.set_route_outcome(db, 999, "down") == 0
    with pytest.raises(ValueError):
        vault.set_route_outcome(db, 41, "meh")
    rows = {int(r["message_id"] or 0): r for r in vault.recent_routes(db)}
    assert rows[41]["runner_up"] == "google_ai_studio" and rows[41]["chaos_profile"] == "pink" and rows[41]["jitter"] == pytest.approx(0.0123)
    assert rows[41]["outcome"] == "up" and rows[42]["outcome"] == "locked"
    stats = {r["route"]: r for r in vault.route_stats(db)}
    assert stats["groq/gpt-oss-120b"]["up"] == 1 and stats["google_ai_studio/gemini"]["locked"] == 1
    csv = vault.routes_csv(db)
    assert ",runner_up,chaos_gain,chaos_profile,jitter,outcome" in csv.splitlines()[0] and ",google_ai_studio,0.25,pink,0.0123,up" in csv
    comparison = {r["setting"]: r for r in vault.chaos_comparison(db)}
    assert comparison["chaos on"]["sends"] == 1 and comparison["chaos on"]["good_rate"] == 1.0 and comparison["chaos on"]["runner_up_differs"] == 1
    assert comparison["chaos off"]["sends"] == 2 and comparison["chaos off"]["failed"] == 1 and comparison["chaos off"]["truncated"] == 1 and comparison["chaos off"]["locked"] == 1
    assert "scope-other" not in str(vault.chaos_comparison("scope-other"))


def test_milp_reports_the_runner_up_and_the_wave_state(db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    plain = select_milp_endpoint("chat", 500, entropy_by_endpoint=zero)
    assert plain.endpoint.name == "groq" and plain.runner_up == "google_ai_studio" and plain.chaos == {} and "runner_up=google_ai_studio" in plain.reason
    pinkwave.activate(db, pinkwave.ChaosSettings(gain=0.5))
    first = select_milp_endpoint("chat", 500, entropy_by_endpoint=zero)
    second = select_milp_endpoint("chat", 500, entropy_by_endpoint=zero)
    assert first.chaos["gain"] == 0.5 and first.chaos["profile"] == "pink" and second.chaos["step"] == first.chaos["step"] + 1
    assert 0.0 <= first.chaos["jitter"] <= 0.5 * pinkwave.ROUTING_MAX_JITTER
    only_groq = select_milp_endpoint("chat", 500, entropy_by_endpoint=zero, excluded=["google_ai_studio"])
    assert only_groq.runner_up == ""  # nothing else was feasible
    assert RouteDecision("p", "m", "chat", "r").runner_up == "" and RouteDecision("p", "m", "chat", "r").chaos == {}


# ----------------------------------------------------------------------------- FTS recall

def seed_summary(scope, thread_id, content, age_days=0.0):
    with vault._open_database() as connection:
        cursor = connection.execute(
            "INSERT INTO summaries (project_scope, covers_from_id, covers_to_id, message_count, content, method, created_at, thread_id) VALUES (?, 1, 2, 2, ?, 'extractive', ?, ?)",
            (scope, content, time.time() - age_days * 86_400, int(thread_id)),
        )
        summary_id = int(cursor.lastrowid)
        vault._index_text(connection, scope, vault._thread_label(connection, thread_id), "summary", thread_id, summary_id, content, time.time() - age_days * 86_400)
    return summary_id


def test_fts_index_is_fed_by_missions_artifacts_and_summaries_and_ranks_by_bm25(db):
    assert vault.recall_index_available()
    a = vault.create_thread(db, "Rollout", workspace="normal_chat")
    b = vault.create_thread(db, "Now", workspace="normal_chat")
    vault.set_thread_mission(a, "Ship the pgbouncer rollout with connection pooling for checkout", db)
    vault.save_artifact(db, "pooling.py", "svc/pooling.py", "def pool_checkout_connections():\n    return 1\n", "python")
    seed_summary(db, a, "Decided: pgbouncer fronts the checkout database; the pool size is 40.\nUnrelated: lunch was fine.")
    recalled = vault.recall_memory(db, "what pool size did we pick for pgbouncer?", b, 2_000)
    assert recalled.startswith(vault.RECALL_PREFIX) and "pool size is 40" in recalled and "lunch" not in recalled
    assert 'mission of chat "Rollout"' in recalled and "pgbouncer rollout" in recalled
    assert "artifact pooling.py" in vault.recall_memory(db, "pool_checkout_connections", b, 2_000)  # identifiers survive the tokenizer
    assert vault.recall_memory(db, "pgbouncer pool", a, 2_000).count("pool size is 40") == 0  # a chat's live summary is not recalled into itself
    assert vault.recall_memory("scope-other", "pgbouncer pool", None, 2_000) == ""


def test_recall_decays_with_age_and_halves_superseded_lines(db):
    a = vault.create_thread(db, "Old", workspace="normal_chat")
    b = vault.create_thread(db, "New", workspace="normal_chat")
    c = vault.create_thread(db, "Asking", workspace="task_finder")
    seed_summary(db, a, "Decision: the cache TTL for checkout is 300 seconds.", age_days=120.0)
    seed_summary(db, b, "Decision: the cache TTL for checkout is 60 seconds.", age_days=1.0)
    lines = vault.recall_memory(db, "checkout cache TTL decision", c, 2_000).splitlines()[1:]
    assert "60 seconds" in lines[0] and "300 seconds" in lines[1]
    with vault._open_database() as connection:
        connection.execute("UPDATE recall_index SET superseded = 1 WHERE thread_id = ?", (b,))
        connection.execute("UPDATE recall_index SET created_at = ? WHERE thread_id = ?", (time.time(), a))
    lines = vault.recall_memory(db, "checkout cache TTL decision", c, 2_000).splitlines()[1:]
    assert "300 seconds" in lines[0]  # fresh beats superseded at equal age


def test_migration_supersedes_the_old_chat_and_rebuild_restores_the_index(db):
    a = vault.create_thread(db, "Design", workspace="normal_chat")
    for i in range(6):
        vault.append_message(db, "user", f"We decided the deploy kit targets oracle-vm, step {i}.", thread_id=a)
        vault.append_message(db, "assistant", "Noted the oracle-vm decision.", thread_id=a)
    seed_summary(db, a, "Decision: the deploy kit targets oracle-vm with caddy in front.")
    result = vault.migrate_thread(db, a)
    with vault._open_database() as connection:
        superseded = connection.execute("SELECT COUNT(*) FROM recall_index WHERE thread_id = ? AND superseded = 1", (a,)).fetchone()[0]
        digests = connection.execute("SELECT COUNT(*) FROM recall_index WHERE kind = 'digest' AND superseded = 0").fetchone()[0]
    assert superseded >= 1 and digests >= 1
    other = vault.create_thread(db, "Later", workspace="normal_chat")
    assert "digest thread-" in vault.recall_memory(db, "deploy kit oracle-vm caddy", other, 2_000)
    with vault._open_database() as connection:
        connection.execute("DELETE FROM recall_index")
    assert vault.recall_memory(db, "deploy kit oracle-vm caddy", other, 2_000) == ""
    assert vault.rebuild_recall_index(db) > 0
    rebuilt = vault.recall_memory(db, "deploy kit oracle-vm caddy", other, 2_000)
    assert "oracle-vm" in rebuilt and result["new_thread_id"] != a
    vault.delete_thread(a, db)
    with vault._open_database() as connection:
        assert connection.execute("SELECT COUNT(*) FROM recall_index WHERE thread_id = ?", (a,)).fetchone()[0] == 0


def test_keyword_fallback_serves_when_fts_is_unavailable(db, monkeypatch):
    a = vault.create_thread(db, "Planning", workspace="normal_chat")
    b = vault.create_thread(db, "Now", workspace="normal_chat")
    seed_summary(db, a, "Constraint: the worker runs with two threads on the oracle vm.")
    monkeypatch.setitem(vault._FTS, "available", False)
    recalled = vault.recall_memory(db, "how many worker threads on the oracle vm", b, 2_000)
    assert "two threads" in recalled and 'chat "Planning"' in recalled
