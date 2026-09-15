"""Batch R: feasibility verdicts. A release wave and a mission are commitments that run whole or not at all; the
verdict says whether today's tokens after the chat reserve cover them, by how much they fall short, and which
vendor's supply binds. With the engine the answer is a verified solve; without it a greedy check by value per token."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import treasury_plan, vault  # noqa: E402
from orchestrator.quota_registry import get_quota_ledger  # noqa: E402
from orchestrator.society import release, store, templates  # noqa: E402
from orchestrator.treasury_plan import Commitment  # noqa: E402

ENGINE = treasury_plan.available()
ITEMS = [
    Commitment("a", "A", 30_000, 1.0, mix={"gemini": 1.0}),
    Commitment("b", "B", 30_000, 1.0, mix={"groq": 1.0}),
    Commitment("c", "C", 30_000, 1.0, mix={"groq": 1.0}),
]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    vault.initialize_database()
    return "scope-r"


def test_spendable_today_takes_the_chat_reserve_off_every_vendor(db):
    pooled, per_vendor = treasury_plan.spendable_today(db, get_quota_ledger())
    capacities = treasury_plan.vendor_capacities(get_quota_ledger())
    assert set(per_vendor) == {"gemini", "groq"} and 0 < pooled < sum(capacities.values())
    assert all(per_vendor[v] < capacities[v] for v in capacities) and sum(per_vendor.values()) <= pooled + 2


def test_greedy_fallback_answers_fits_short_and_supply(db, monkeypatch):
    monkeypatch.setattr(treasury_plan, "available", lambda: False)
    fits = treasury_plan.feasibility(db, get_quota_ledger(), ITEMS, "mission", budget=100_000, capacities={"gemini": 50_000, "groq": 80_000})
    assert fits.fits and fits.status == "engine-missing" and fits.cost_tokens == 90_000 and fits.unfunded == [] and "fits today" in fits.summary()
    short = treasury_plan.feasibility(db, get_quota_ledger(), ITEMS, "mission", budget=50_000, capacities={"gemini": 50_000, "groq": 80_000})
    assert not short.fits and short.status == "short" and short.shortfall_tokens == 40_000 and len(short.unfunded) == 2 and "short by 40,000" in short.summary()
    supply = treasury_plan.feasibility(db, get_quota_ledger(), ITEMS, "mission", budget=100_000, capacities={"gemini": 50_000, "groq": 30_000})
    assert not supply.fits and set(supply.unfunded) == {"c"} or set(supply.unfunded) == {"b"}
    assert treasury_plan.feasibility(db, get_quota_ledger(), [], "wave").status == "empty"


@pytest.mark.skipif(not ENGINE, reason="scrcae not installed")
def test_engine_verdicts_name_the_shortfall_and_the_binding_vendor(db):
    fits = treasury_plan.feasibility(db, get_quota_ledger(), ITEMS, "mission", budget=100_000, capacities={"gemini": 50_000, "groq": 80_000})
    assert fits.fits and fits.status == "fits" and fits.funded == ["a", "b", "c"] and fits.engine and fits.input_hash
    assert {r["vendor"]: r["headroom"] for r in fits.resources} == {"gemini": 20_000, "groq": 20_000}
    short = treasury_plan.feasibility(db, get_quota_ledger(), ITEMS, "mission", budget=50_000, capacities={"gemini": 50_000, "groq": 80_000})
    assert not short.fits and short.status == "short" and short.shortfall_tokens == 40_000 and len(short.funded) == 1 and "budget" in short.diagnosis.lower()
    if treasury_plan.engine_knows_resources():
        supply = treasury_plan.feasibility(db, get_quota_ledger(), ITEMS, "mission", budget=100_000, capacities={"gemini": 50_000, "groq": 30_000})
        assert not supply.fits and supply.status == "supply" and supply.limited_by == "groq" and "supply" in supply.summary()
        assert supply.funded == ["a", "b"] or supply.funded == ["a", "c"]


def test_mission_feasibility_counts_model_and_sub_mission_steps_in_order(db):
    plan = [
        {"id": 1, "title": "Brief", "executor": "model"},
        {"id": 2, "title": "Solve", "executor": "solver"},
        {"id": 3, "title": "Sections", "executor": "sub_mission", "config": {"sections": 2}},
        {"id": 4, "title": "Notes", "executor": "model"},
    ]
    normal = treasury_plan.mission_feasibility(db, get_quota_ledger(), plan, 2048, heavy=False, budget=10_000_000, capacities={"gemini": 5_000_000, "groq": 5_000_000})
    assert normal.fits and normal.funded == ["step:1", "step:3", "step:4"]
    heavy = treasury_plan.mission_feasibility(db, get_quota_ledger(), plan, 2048, heavy=True, budget=10_000_000, capacities={"gemini": 5_000_000, "groq": 5_000_000})
    assert heavy.cost_tokens > normal.cost_tokens and heavy.fits
    tiny = treasury_plan.mission_feasibility(db, get_quota_ledger(), plan, 2048, heavy=False, budget=3_000, capacities={"gemini": 1_500, "groq": 1_500})
    assert not tiny.fits and tiny.shortfall_tokens > 0 and len(tiny.unfunded) == 3


def test_wave_feasibility_prices_each_unfinished_work_by_its_stage(db):
    templates.seed_company(db, "avs_studio")
    company_id = int(store.companies_for(db)[0]["id"])
    items, status = treasury_plan.wave_items(db, company_id, call_tokens=1_000)
    assert status["wave"] == 1 and len(items) == int(status["works"]) and all(i.cost_tokens > 0 for i in items)
    assert all(abs(i.value_points - 100.0 / status["needed"]) < 1e-9 for i in items) and all(abs(sum(i.mix.values()) - 1.0) < 1e-9 for i in items)
    first = items[0]
    store.update("catalog", int(first.key.split(":")[1]), stage="edit")
    cheaper, _ = treasury_plan.wave_items(db, company_id, call_tokens=1_000)
    assert cheaper[0].cost_tokens == treasury_plan.STAGE_CALLS["edit"] * 2_000 < first.cost_tokens
    store.update("catalog", int(first.key.split(":")[1]), stage="final")
    fewer, _ = treasury_plan.wave_items(db, company_id, call_tokens=1_000)
    assert len(fewer) == len(items) - 1  # a final work needs nothing more
    verdict, status = treasury_plan.wave_feasibility(db, get_quota_ledger(), company_id, call_tokens=1_000)
    assert verdict.kind == "wave" and verdict.budget_tokens > 0 and verdict.cost_tokens == sum(i.cost_tokens for i in fewer)
    assert verdict.fits or (verdict.shortfall_tokens > 0 or verdict.limited_by)
    # A plan of the day with a company line bounds the wave's budget to that line.
    plan = treasury_plan.DayPlan(db, "2026-09-15", 1_000_000, 60.0, "optimal", [treasury_plan.PlanLine(f"company:{company_id}", "AVS", 0.3, 12_000, 7.5)])
    vault.setting_set(db, treasury_plan.SETTING_LATEST, plan.to_json())
    bounded, _ = treasury_plan.wave_feasibility(db, get_quota_ledger(), company_id, call_tokens=1_000)
    assert bounded.budget_tokens == 12_000 and not bounded.fits and bounded.unfunded
    assert release.wave_status(db, company_id)["gate_met"] is False
