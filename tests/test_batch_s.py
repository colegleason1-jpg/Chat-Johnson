"""Batch S: goal points fitted from evidence. Each activity's typed points are a prior; last week's successes per
thousand tokens (verdicts, finished work, passed evaluations, dream-bank entries) reweight them, blended by how much
evidence there is, and an activity without enough evidence keeps its typed number and says why."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import treasury_plan, vault  # noqa: E402
from orchestrator.quota_registry import get_quota_ledger  # noqa: E402
from orchestrator.society import store, templates  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    vault.initialize_database()
    return "scope-s"


def _chat_verdicts(scope, thread, good, bad, tokens_each=500):
    for i in range(good + bad):
        message_id = vault.append_message(scope, "assistant", "x" * (tokens_each * 4), thread_id=thread, workspace="normal_chat")
        vault.record_route(scope, "normal_chat", "chat", "groq/m", "normal", 100, "", "r", message_id=message_id)
        vault.set_route_outcome(scope, message_id, "up" if i < good else "down")


def _cycles(scope, kind, count, tokens, company_id=None):
    now = time.time()
    for _ in range(count):
        store.insert("cycles", scope, kind=kind, company_id=company_id, job_id=None, started_at=now - 60, finished_at=now, tokens_planned=tokens, tokens_used=tokens, calls=3, log=[], status="done")


def test_without_evidence_every_activity_keeps_its_typed_prior_and_says_why(db):
    rows = {r["activity"]: r for r in treasury_plan.fit_values(db)}
    assert set(rows) == {"chat", "company", "academy", "leisure", "missions"}
    assert rows["chat"]["quality"].startswith("too few observations (0 of 12)") and rows["chat"]["fitted"] == treasury_plan.DEFAULT_VALUE_POINTS["chat"]
    assert rows["missions"]["quality"] == "not measured" and all(r["weight"] == 0.0 for r in rows.values())
    values, _ = treasury_plan.effective_values(db)
    assert values == treasury_plan.DEFAULT_VALUE_POINTS


def test_yields_reweight_the_priors_and_keep_their_total(db):
    templates.seed_company(db, "avs_studio")
    company_id = int(store.companies_for(db)[0]["id"])
    thread = vault.create_thread(db, "t", workspace="normal_chat")
    _chat_verdicts(db, thread, good=15, bad=5, tokens_each=500)          # 15 good over 10,000 tokens: 1.5 per 1k
    _cycles(db, "company", 4, 20_000, company_id)                         # 80,000 tokens
    for i in range(8):                                                    # 8 items done: 0.1 per 1k
        store.add_work_item(db, company_id, f"item {i}", "brief", status="done")
    _cycles(db, "academy", 3, 10_000)                                     # 30,000 tokens, 0 passes: no successes
    _cycles(db, "leisure", 2, 1_000)                                      # too few cycles
    rows = {r["activity"]: r for r in treasury_plan.fit_values(db)}
    chat_tokens = sum(int(r["token_count"]) for r in vault.recent_messages(db, 50, thread_id=thread) if r["role"] == "assistant")
    assert rows["chat"]["quality"] == "fitted" and rows["chat"]["observations"] == 20 and rows["chat"]["successes"] == 15 and rows["chat"]["tokens"] == chat_tokens > 0
    chat_yield = round(15 / chat_tokens * 1000, 4)
    assert rows["chat"]["yield_per_1k"] == chat_yield and rows["company"]["yield_per_1k"] == 0.1 and rows["company"]["quality"] == "fitted"
    assert rows["academy"]["quality"] == "no successes" and rows["leisure"]["quality"].startswith("too few observations (2 of 3)")
    # Shares: the two fitted activities split their prior total (30 + 25 = 55) by yield, then blend by n / (n + 10).
    total_yield = 15 / chat_tokens * 1000 + 0.1
    chat_share, company_share = 55 * (15 / chat_tokens * 1000) / total_yield, 55 * 0.1 / total_yield
    w_chat, w_company = 20 / 30, 4 / 14
    assert rows["chat"]["fitted"] == pytest.approx(w_chat * chat_share + (1 - w_chat) * 30, abs=1e-3) and rows["chat"]["weight"] == round(w_chat, 3)
    assert rows["company"]["fitted"] == pytest.approx(w_company * company_share + (1 - w_company) * 25, abs=1e-3)
    assert rows["academy"]["fitted"] == 18.0 and rows["academy"]["weight"] == 0.0
    values, _ = treasury_plan.effective_values(db)
    assert values["chat"] == rows["chat"]["fitted"] and values["company"] == rows["company"]["fitted"] and values["academy"] == 18.0
    # The setting turns the fit off without losing the evidence table.
    treasury_plan.save_config(db, 0.75, 0.15, use_fitted=False)
    typed, table = treasury_plan.effective_values(db)
    assert typed["chat"] == 30.0 and {r["activity"]: r["quality"] for r in table}["chat"] == "fitted"
    # A single fitted activity cannot be reweighted against nothing: it keeps its prior and the row says so.
    store.update("companies", company_id, status="archived")
    with vault._open_database() as connection:
        connection.execute("DELETE FROM work_items WHERE project_scope = ?", (db,))
    alone = {r["activity"]: r for r in treasury_plan.fit_values(db)}
    assert alone["chat"]["fitted"] == 30.0 and alone["chat"]["quality"].startswith("fitted (needs a second")


def test_the_plan_uses_fitted_points_and_records_their_provenance(db):
    templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 10)
    company_id = int(store.companies_for(db)[0]["id"])
    thread = vault.create_thread(db, "t", workspace="normal_chat")
    _chat_verdicts(db, thread, good=20, bad=0, tokens_each=250)           # 4 per 1k: the chat earns more points
    _cycles(db, "company", 5, 50_000, company_id)
    store.add_work_item(db, company_id, "one", "brief", status="done")    # 0.004 per 1k
    activities, _, _ = treasury_plan.build_activities(db, get_quota_ledger())
    by_key = {a.key: a for a in activities}
    assert by_key["chat"].value_points > treasury_plan.DEFAULT_VALUE_POINTS["chat"] and by_key[f"company:{company_id}"].value_points < treasury_plan.DEFAULT_VALUE_POINTS["company"]
    assert by_key["academy"].value_points == treasury_plan.DEFAULT_VALUE_POINTS["academy"]  # no evidence: typed
    plan = treasury_plan.plan_day(db, get_quota_ledger(), with_tail=False, with_stress=False)
    quality = {r["activity"]: r["quality"] for r in plan.value_rows}
    assert quality["chat"] == "fitted" and quality["company"] == "fitted" and quality["academy"].startswith("too few observations")
    assert treasury_plan.latest(db)["value_rows"][0]["activity"] == "chat"
