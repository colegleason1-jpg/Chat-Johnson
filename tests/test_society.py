"""Society layer: run_after scheduling, org templates, personas, the economy, and a full company cycle with a fake model."""
import json
import os
import re
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import jobs, vault  # noqa: E402
from orchestrator.router import RouteDecision  # noqa: E402
from orchestrator.society import cycles, economy, evaluate, store, templates  # noqa: E402
from orchestrator.society.personas import board_persona, persona_block  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    yield "scope-s"


def test_run_after_holds_a_queued_job_until_due(db):
    jobs.register_handler("later", lambda ctx: {})
    job_id = jobs.enqueue(db, "later", {}, {}, run_after=time.time() + 3600)
    assert vault.claim_job("t", ("later",)) is None
    vault.update_job_progress(job_id, {})  # touching progress does not release it
    assert vault.claim_job("t", ("later",)) is None
    with vault._open_database() as connection:
        connection.execute("UPDATE jobs SET run_after = 0 WHERE id = ?", (job_id,))
    assert vault.claim_job("t", ("later",))["id"] == job_id
    assert vault.job_view(vault.job_by_id(job_id))["run_after"] == 0.0


def test_templates_seed_both_companies_from_one_society(db):
    avs = templates.seed_company(db, "avs_studio")
    sw = templates.seed_company(db, "software_co")
    assert templates.seed_company(db, "avs_studio") == avs  # idempotent
    seats = store.seats_for(db, avs)
    assert len(seats) == 23 and len(store.seats_for(db, avs, "filled")) == 19  # marketing and sales open until wave 1 ships
    assert all(3 <= len(store.load_json(s["roles"], [])) <= 5 for s in seats)
    catalog = store.catalog_for(db, avs)
    assert len(catalog) == 17 and sum(1 for c in catalog if c["release_wave"] == 1) == 6
    items = store.work_items_for(db, avs)
    assert len(items) == 14 and sum(1 for i in items if i["importance"] == 5) == 7
    assert len(store.seats_for(db, sw)) == 17 and len(store.catalog_for(db, sw)) == 3 and len(store.work_items_for(db, sw)) == 21
    assert len(store.agents_for(db, tier="philosopher", employment="seated")) == 19 + 17
    assert {d["key"] for d in store.departments_for(db, avs) if not d["active"]} == {"marketing", "sales"}
    ceo = store.seat_by_key(db, avs, "ceo")
    ea = store.seat_by_key(db, avs, "ea_board")
    assert ceo["reports_to"] == ea["id"] and ea["reports_to"] is None
    assert store.row("agents", ceo["agent_id"])["seat_id"] == ceo["id"]
    templates.add_product(db, sw, "widget", "Widget", "does widgets")
    assert len(store.catalog_for(db, sw)) == 4 and len(store.work_items_for(db, sw)) == 28 and store.seat_by_key(db, sw, "pm_widget")["agent_id"]


def test_persona_block_names_roles_vision_and_skip_level_seats(db):
    avs = templates.seed_company(db, "avs_studio")
    company = store.row("companies", avs)
    seats = {int(s["id"]): s for s in store.seats_for(db, avs)}
    lead = store.seat_by_key(db, avs, "production_lead")
    agent = store.row("agents", lead["agent_id"])
    block = persona_block(agent, lead, company, seats, dream_excerpt="Tides follow the moon.")
    assert "SEAT: Head of Production at AVS Studio" in block and "schedule drafts" in block
    assert "REPORTS TO: Executive Assistant to the CEO (ea_ceo)" in block
    assert "one seat above your superior (CEO (ceo))" in block and "Writer (writer_1)" in block  # one above, one below
    assert "COMPANY VISION" in block and "craft over volume" in block and "Tides follow the moon" in block
    persona = board_persona(company, ["work items: 3 backlog"])
    assert "FILTER: idea|directive|question|chat" in persona and "work items: 3 backlog" in persona


def test_economy_credits_real_tokens_and_never_overdraws(db):
    agent = store.add_agent(db, "A", "p", tier="producer", allowance=100)
    decision = RouteDecision("groq", "m", "chat", "r")
    earned = economy.charge(db, agent, [{"role": "user", "content": "x" * 400}], "y" * 400, decision, cycle_id=None)
    assert earned == 200 and store.ledger_balance(db, agent) == 200
    assert economy.debit(db, agent, 500, "leisure") == 200 and store.ledger_balance(db, agent) == 0
    assert economy.allowance_run(db) == 100 and store.ledger_balance(db, agent) == 100
    kinds = [r["kind"] for r in store.rows("token_ledger", db)]
    assert kinds == ["earn", "spend", "allowance"]
    check = evaluate.deterministic_check("Write the story bible with premise, world, characters, tone", "story bible premise world characters tone " * 25)
    assert check["passed"] and check["coverage"] == 1.0
    assert not evaluate.deterministic_check("brief", "TODO placeholder text " * 30)["passed"]


def fake_model(calls):
    def generate_mode(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        prompt = messages[-1]["content"]
        calls.append((task_type, prompt[:60]))
        if "Level 10 meeting" in prompt:
            text = "HEADLINE: Outlines are moving\nISSUE: Two writers idle | RESOLUTION: assign chapter briefs\nTODO: production_lead: rebalance writers\nQUESTION: Which work leads wave 1?"
        elif "Rate each backlog" in prompt:
            text = "\n".join(f"#{i}: 5" for i in re.findall(r"#(\d+) ·", prompt))
        elif "Assign each item" in prompt:
            ids = re.findall(r"#(\d+) ·", prompt)
            text = "\n".join(f"#{i} -> {'analytics_lead' if n == 0 else 'lead_writer_1'}" for n, i in enumerate(ids))
        elif "Break this item" in prompt:
            text = "- Premise and world :: Write the premise and world in two paragraphs.\n- Character sheet :: Describe three principal characters."
        elif "Review the deliverable" in prompt:
            text = "PASS\nClear and on brief."
        elif "report for the board" in prompt:
            text = "Cycle report: outlines moved, one item waits for the board."
        else:
            text = ("story bible premise world characters tone outline chapter open questions board " * 12)
        return text, RouteDecision("fake", "m1", task_type, "test route")
    return generate_mode


def run_cycle(scope, company_id, monkeypatch, calls, **kwargs):
    monkeypatch.setattr(cycles, "generate_mode", fake_model(calls))
    monkeypatch.setattr(cycles, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    monkeypatch.setattr(economy.Treasury, "daily_remaining", lambda self: 10_000_000)
    job_id = cycles.run_now(scope, company_id, {"GEMINI_API_KEY": "AIza-fake"}, **kwargs)
    jobs.run_job(vault.claim_job("t", (cycles.KIND_COMPANY,)))
    return job_id, vault.job_view(vault.job_by_id(job_id))


def test_company_cycle_moves_work_through_the_pipeline(db, monkeypatch):
    avs = templates.seed_company(db, "avs_studio")
    board = int(vault.active_thread(db, "company")["id"])
    calls = []
    job_id, view = run_cycle(db, avs, monkeypatch, calls, chain=True, board_thread_id=board, call_tokens=300)
    assert view["status"] == "done", view
    result = view["result"]
    assert result["status"] == "done" and result["calls"] == len(calls) and 8 <= result["calls"] <= 31
    steps = [entry["step"] for entry in result["log"]]
    assert steps == ["scorecard", "l10", "rate", "delegate", "analytics", "execute", "review", "report"]
    statuses = {}
    for item in store.work_items_for(db, avs, limit=500):
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    assert statuses.get("backlog", 0) == 4 and statuses.get("board", 0) >= 1 and statuses.get("done", 0) >= 1  # 10 rated of 14; one broken down
    assert any(i["parent_id"] for i in store.work_items_for(db, avs, limit=500))  # analytics created sub-items
    assert store.count("issues", db) == 1 and store.count("todos", db) == 1 and store.count("meetings", db) == 1
    assert store.count("scorecard", db) >= 19 and store.count("evaluations", db) >= 1
    assert sum(r["amount"] for r in store.rows("token_ledger", db, "kind = 'earn'")) == result["tokens"] > 0
    inbox = vault.recent_messages(db, 5, thread_id=board)
    assert inbox and inbox[-1]["provider"] == "ea_board" and "Cycle report" in inbox[-1]["content"]
    lead = store.seat_by_key(db, avs, "lead_writer_1")
    assert lead["thread_id"] and vault.recent_messages(db, 5, thread_id=int(lead["thread_id"]))
    assert any(c["stage"] in ("draft", "preliminary_review") for c in store.catalog_for(db, avs))
    cycle = store.cycles_for(db, avs, limit=1)[0]
    assert cycle["status"] == "done" and cycle["next_run_after"] and cycle["next_run_after"] > time.time() + 3000
    queued = vault.list_jobs(db, ("queued",), kind=cycles.KIND_COMPANY)
    assert len(queued) == 1 and vault.job_view(queued[0])["payload"]["chained_from"] == job_id
    assert "AIza-fake" not in json.dumps(view) and vault.claim_job("t", (cycles.KIND_COMPANY,)) is None  # successor waits for its time


def test_company_cycle_stops_cleanly_when_the_budget_runs_out(db, monkeypatch):
    avs = templates.seed_company(db, "avs_studio")
    calls = []
    _, view = run_cycle(db, avs, monkeypatch, calls, max_calls=2)
    assert view["status"] == "done" and view["result"]["status"] == "budget" and view["result"]["calls"] == 2
    assert view["result"]["log"][-1]["step"] in ("rate", "delegate") and "budget exhausted" in view["result"]["log"][-1]["stopped"]
    assert store.cycles_for(db, avs, limit=1)[0]["status"] == "budget"
    assert not store.work_items_for(db, avs, ("running",))  # nothing left half-done
