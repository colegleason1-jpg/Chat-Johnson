"""Batch G2: society cost hygiene, EOS semantics, release waves and manuscripts, wake-for-seat, teaching, revert safety, UI fixes."""
import os
import re
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import github_push as gp, jobs, preview, vault  # noqa: E402
from orchestrator.router import ProviderError, RouteDecision  # noqa: E402
from orchestrator.society import academy, cycles, economy, eos, release, store, templates, tick  # noqa: E402
from orchestrator.society.personas import persona_block  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    monkeypatch.setattr(economy.Treasury, "daily_remaining", lambda self: 10_000_000)
    yield "scope-g2"


def quiet(monkeypatch):
    monkeypatch.setattr(cycles, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    monkeypatch.setattr(academy, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)


def run_company(db, company_id, monkeypatch, model, **kwargs):
    monkeypatch.setattr(cycles, "generate_mode", model)
    quiet(monkeypatch)
    job_id = cycles.run_now(db, company_id, {"GEMINI_API_KEY": "AIza-fake"}, **kwargs)
    jobs.run_job(vault.claim_job("t", (cycles.KIND_COMPANY,)))
    return vault.job_view(vault.job_by_id(job_id))


# ----------------------------------------------------------------------------- cost hygiene

def test_a_provider_error_mid_cycle_is_recorded_and_the_tick_backs_off(db, monkeypatch):
    avs = templates.seed_company(db, "avs_studio")

    def outage(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        raise ProviderError("gemini HTTP 429: rate limited")

    view = run_company(db, avs, monkeypatch, outage)
    assert view["status"] == "failed" and "429" in view["result"]["error"]
    cycle = store.cycles_for(db, avs, limit=1)[0]
    assert cycle["status"] == "failed" and cycle["finished_at"] and "rate-limited" in cycle["log"]
    assert not store.work_items_for(db, avs, ("running",))
    started, failures = tick._last_attempt(db, "company", avs)
    assert failures == 1 and started > 0
    interval = float(store.row("companies", avs)["interval_s"])
    assert not tick._due(db, "company", avs, interval, time.time())  # not due: the interval has not passed once, let alone doubled
    assert tick._due(db, "company", avs, interval, time.time() + interval * 2 + 1)
    assert not tick._due(db, "company", avs, interval, time.time() + interval + 1)  # one failure doubles the wait
    store.update("cycles", int(cycle["id"]), status="done")
    assert tick._due(db, "company", avs, interval, time.time() + interval + 1)  # a success resets the backoff


def test_a_zero_treasury_makes_no_call_and_shares_come_from_the_board(db, monkeypatch):
    avs = templates.seed_company(db, "avs_studio")
    sw = templates.seed_company(db, "software_co")
    monkeypatch.setattr(economy.Treasury, "daily_remaining", lambda self: 0)
    calls = []

    def counting(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        calls.append(task_type)
        return "x", RouteDecision("fake", "m", task_type, "r")

    view = run_company(db, avs, monkeypatch, counting)
    assert view["status"] == "done" and view["result"]["status"] == "budget" and calls == []
    assert store.cycles_for(db, avs, limit=1)[0]["status"] == "budget"
    monkeypatch.setattr(economy.Treasury, "daily_remaining", lambda self: 100_000)
    store.update("companies", avs, daily_share=0.05)
    store.update("companies", sw, daily_share=0.15)
    shares = economy.shares_for(db)
    assert shares["company"] == 0.05 and shares["company_2"] == 0.15 and abs(shares["academy"] - 0.5) < 0.01 and abs(shares["leisure"] - 0.3) < 0.01
    treasury = economy.Treasury(vault_ledger := __import__("orchestrator.quota", fromlist=["QuotaLedger"]).QuotaLedger({}), shares)
    assert treasury.cycle_budget("company", 80_000, share=0.05) == 5_000 and treasury.cycle_budget("academy", 80_000) == 50_000
    assert economy.keyed_vendors() == () or "local" not in economy.keyed_vendors()
    _ = vault_ledger


def test_allowances_are_funded_from_the_academy_budget_and_paid_to_free_agents_only(db):
    templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 30)
    seated = {int(a["id"]) for a in store.agents_for(db, employment="seated")}
    fired = store.add_agent(db, "Gone", "p", tier="producer", allowance=500, employment="fired")
    granted = economy.allowance_run(db, None, max_total=1_000)
    assert 0 < granted <= 1_000
    rows = store.rows("token_ledger", db, "kind = 'allowance'")
    assert rows and not any(int(r["agent_id"]) in seated or int(r["agent_id"]) == fired for r in rows)


def test_start_tick_never_starts_a_second_chain(db):
    first = tick.start_tick(db, {"GEMINI_API_KEY": "AIza-fake"}, interval_s=600)
    assert tick.start_tick(db, {"GEMINI_API_KEY": "AIza-fake"}, interval_s=600) == first
    assert len(vault.list_jobs(db, ("queued",), kind=tick.KIND_TICK)) == 1


# ----------------------------------------------------------------------------- EOS semantics

def test_every_seeded_kpi_is_reachable_by_the_seat_that_owns_it(db):
    avs = templates.seed_company(db, "avs_studio")
    sw = templates.seed_company(db, "software_co")
    for company in (avs, sw):
        for seat in store.seats_for(db, company):
            kpis = store.load_json(seat["kpis"], {})
            for kpi in kpis:
                if kpi == "reviews":
                    assert seat["key"] in ("managing_editor", "qa_reviewer"), seat["key"]  # only the reviewer seat grades
                elif kpi == "deliverables":
                    assert seat["key"] not in cycles.EXEC_KEYS, seat["key"]  # executives never receive delegated work
                else:
                    assert kpi in ("reports", "review_pass_rate"), (seat["key"], kpi)


def test_scorecard_uses_a_rolling_week_one_row_per_kpi_and_a_hold_buys_a_week(db, monkeypatch):
    avs = templates.seed_company(db, "avs_studio")
    writer = store.seat_by_key(db, avs, "writer_1")
    item = store.add_work_item(db, avs, "chapter", "b", 3, seat_id=int(writer["id"]))
    store.set_work_status(item, "done")
    with vault._open_database() as connection:
        connection.execute("UPDATE work_items SET updated_at = ? WHERE id = ?", (time.time() - 3 * 86_400, item))  # last Thursday counts this Monday
    cycle_id = store.start_cycle(db, "company", avs, None, 0)
    misses = eos.scorecard_review(db, avs, cycle_id)
    assert not any(m["seat"] == "writer_1" for m in misses)
    eos.scorecard_review(db, avs, cycle_id)  # a second review in the same week replaces its rows
    assert store.count("scorecard", db, "seat_id = ? AND kpi = 'deliverables'", (int(writer["id"]),)) == 1
    assert store.rows("scorecard", db, "seat_id = ? AND kpi = 'reports'", (int(store.seat_by_key(db, avs, "ceo")["id"]),))[0]["actual"] == 1.0
    # HOLD: the seat missed two weeks, the CEO holds, and the count drops to one so the question waits for a new missed week.
    store.update("seats", int(writer["id"]), miss_weeks=1, miss_week="2000-W01")
    store.set_work_status(item, "backlog")

    def holding(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        prompt = messages[-1]["content"]
        if "Personnel decision" in prompt:
            return "HOLD the writer is new", RouteDecision("fake", "m", task_type, "r")
        return "HEADLINE: fine", RouteDecision("fake", "m", task_type, "r")

    view = run_company(db, avs, monkeypatch, holding, max_calls=3)
    personnel = next(e for e in view["result"]["log"] if e["step"] == "personnel")
    seat = store.row("seats", int(writer["id"]))
    assert personnel["held"] == 1 and personnel["fired"] == 0 and seat["miss_weeks"] == 1 and seat["miss_week"] == eos.iso_week()
    # The reviewer seat is never fired.
    editor = store.seat_by_key(db, avs, "managing_editor")
    store.update("seats", int(editor["id"]), miss_weeks=5, miss_week="2000-W01")
    view = run_company(db, avs, monkeypatch, holding, max_calls=3)
    assert store.row("seats", int(editor["id"]))["status"] == "filled"


def test_minutes_update_rocks_and_todos_and_stages_complete_milestones(db):
    avs = templates.seed_company(db, "avs_studio")
    seats = {s["key"]: s for s in store.seats_for(db, avs)}
    store.insert("todos", db, company_id=avs, seat_id=int(seats["production_lead"]["id"]), text="rebalance writers across teams", due_at=time.time() + 86_400, created_at=time.time())
    minutes = eos.parse_minutes("HEADLINE: ok\nROCK: Draft wave-1 works to editorial pass | off_track\nDONE: production_lead: rebalance the writers\nQUESTION: none")
    assert minutes["rock"] and minutes["done"]
    changed = eos.apply_minutes(db, avs, minutes, seats)
    assert changed == {"rocks": 1, "todos": 1}
    assert [r["status"] for r in store.rows("rocks", db, "company_id = ?", (avs,)) if "editorial" in r["title"]] == ["off_track"]
    assert store.rows("todos", db, "company_id = ?", (avs,))[0]["done"] == 1
    assert eos.advance_timeline(db, avs) == 0
    for cat in store.catalog_for(db, avs):
        if cat["release_wave"] == 1:
            store.update("catalog", int(cat["id"]), stage="draft")
    assert eos.advance_timeline(db, avs) == 1  # "Outlines and story bibles done": every wave-1 work is past development
    assert [m["status"] for m in store.rows("timeline", db, "company_id = ?", (avs,), order="due_at ASC")][0] == "done"


def test_grade_verdicts_parse_common_phrasings_and_teaching_notes_reach_the_next_task(db, monkeypatch):
    assert academy.verdict_passes("SCORE: 90\nVerdict: PASS\nnotes") is True
    assert academy.verdict_passes("SCORE: 40\nFAIL.\nthin") is False
    assert academy.verdict_passes("SCORE: 40\nno verdict line") is None
    assert academy.producer_count(40_000, 700) == 8 and academy.producer_count(3_000, 700) == 4 and academy.producer_count(20_000, 700) == 7
    templates.seed_academy(db, 12)
    producer = store.agents_for(db, tier="producer", employment="free")[0]
    prompts = []

    def failing_grader(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        prompt = messages[-1]["content"]
        prompts.append((messages[0]["content"], prompt[:30]))
        if "Auxiliary guardian grading" in prompt:
            return "SCORE: 30\nFAIL\nName the audience first.\nKeep every number.\nAUDIT: ok", RouteDecision("fake", "m", task_type, "r")
        return "short", RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setattr(academy, "generate_mode", failing_grader)
    quiet(monkeypatch)
    for _ in range(2):
        job_id = academy.run_now(db, {"GEMINI_API_KEY": "AIza-fake"})
        jobs.run_job(vault.claim_job("t", (academy.KIND_ACADEMY,)))
        assert vault.job_view(vault.job_by_id(job_id))["status"] == "done"
    note = store.row("agents", int(producer["id"]))["note"]
    assert note.startswith("TEACHING") and "audience" in note
    assert any("guardian's note" in system and "audience" in system for system, _ in prompts)  # the second cycle carried it


def test_failed_exams_rotate_candidates_and_examiners_are_free_philosophers(db):
    templates.seed_company(db, "avs_studio")
    a, b = (store.add_agent(db, f"Aux {i}", "p", tier="auxiliary", allowance=1500) for i in range(2))
    for agent in (a, b):
        for _ in range(3):
            store.insert("evaluations", db, agent_id=int(agent), grader_agent_id=None, kind="task", prompt_key="x", score=0.8, rubric={}, passed=1, timestamp=time.time())
            store.insert("evaluations", db, agent_id=int(agent) + 1000, grader_agent_id=int(agent), kind="task", prompt_key="x", score=0.8, rubric={"agree": True}, passed=1, timestamp=time.time())
    store.insert("evaluations", db, agent_id=int(a), kind="graduation", prompt_key="philosopher_exam", score=0.3, rubric={}, passed=0, timestamp=time.time() - 3600)
    assert academy._last_exam(db, int(a)) > 0 and academy._last_exam(db, int(b)) == 0
    now = time.time()
    candidates = [x for x in store.agents_for(db, tier="auxiliary", employment="free") if academy._agreements(db, int(x["id"])) >= academy.AGREEMENTS_TO_EXAM]
    candidates = [x for x in candidates if now - academy._last_exam(db, int(x["id"])) >= academy.EXAM_COOLDOWN_SECONDS]
    assert [int(x["id"]) for x in candidates] == [int(b)]  # the one who failed an hour ago sits out


# ----------------------------------------------------------------------------- release waves and manuscripts

def finish_work(db, company_id, catalog_id, title, body):
    artifact_id, _ = vault.save_artifact(db, f"{title}.md", f"company/avs_studio/{title}.md", body, "markdown")
    item = store.add_work_item(db, company_id, f"{title} · IP", "b", 4, catalog_id=int(catalog_id))
    store.set_work_status(item, "done", artifact_id=int(artifact_id))
    return item


def test_manuscripts_assemble_in_order_and_publish_uses_the_approved_final(db):
    avs = templates.seed_company(db, "avs_studio")
    cat = store.catalog_for(db, avs)[0]
    finish_work(db, avs, cat["id"], "Story bible", "# Bible\nkeeper and tides")
    finish_work(db, avs, cat["id"], "Draft chapter one", "# One\nthe tides answered")
    rejected = store.add_work_item(db, avs, "Draft chapter one (redo) · IP", "b", 4, catalog_id=int(cat["id"]))
    bad, _ = vault.save_artifact(db, "bad.md", "company/avs_studio/bad.md", "REJECTED DRAFT", "markdown")
    store.set_work_status(rejected, "rejected", artifact_id=int(bad))
    final = release.request_final_edit(db, avs, int(cat["id"]), "Name the keeper.")
    brief = store.row("work_items", final)["brief"]
    assert "THE WORK AS IT STANDS" in brief and "the tides answered" in brief and "Name the keeper" in brief
    finish_work(db, avs, cat["id"], "Final edit with board feedback", "# Final\nMara and the tides")
    published = release.publish_work(db, int(cat["id"]))
    body = vault.export_artifact(published, db)[1]
    assert "REJECTED DRAFT" not in body and body.index("keeper and tides") < body.index("the tides answered") < body.index("Mara and the tides")
    assert body.startswith(f"# {cat['title']}")


def test_wave_one_goes_to_the_board_as_one_release(db):
    avs = templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 30)
    wave = release.wave_works(db, avs, 1)
    assert len(wave) == 6 and release.assemble_wave(db, avs, 1) is None
    for cat in wave:
        finish_work(db, avs, cat["id"], "Story bible", f"# {cat['key']}\ntext")
        store.update("catalog", int(cat["id"]), stage="final")
    status = release.wave_status(db, avs, 1)
    assert status["gate_met"] and status["ready"] == 6
    release_id = release.assemble_wave(db, avs, 1)
    assert release_id and release.assemble_wave(db, avs, 1) == release_id
    assert store.row("releases", release_id)["status"] == "board_review" and all(store.row("catalog", int(c["id"]))["artifact_id"] for c in wave)
    assert release.return_release(db, release_id, "Tighten every opening.") == 6
    assert store.row("releases", release_id)["status"] == "returned"
    assert all(store.row("catalog", int(c["id"]))["stage"] == "board_feedback" for c in wave)
    assert store.count("work_items", db, "title LIKE 'Final edit with board feedback%'") == 6
    for cat in wave:
        store.update("catalog", int(cat["id"]), stage="final")
    second = release.assemble_wave(db, avs, 1)
    assert second and second != release_id
    published = release.approve_release(db, second, "Ship it.")
    assert len(published) == 6 and store.row("releases", second)["status"] == "released"
    assert all(store.row("catalog", int(c["id"]))["stage"] == "published" for c in wave)
    assert all(d["active"] for d in store.departments_for(db, avs))


def test_open_seats_wake_an_exploring_agent_whose_interest_matches(db):
    avs = templates.seed_company(db, "avs_studio")
    writer = store.seat_by_key(db, avs, "writer_1")
    store.unseat_agent(db, int(writer["id"]))
    plain = store.add_agent(db, "Plain", "p", tier="philosopher", allowance=2000)
    explorer = store.add_agent(db, "Explorer", "p", tier="philosopher", allowance=2000, mode="explore", interest="drafting chapters and scenes in a story bible", wake_after=time.time() + 5 * 3600)
    store.insert("evaluations", db, agent_id=int(plain), kind="graduation", prompt_key="x", score=0.99, rubric={}, passed=1, timestamp=time.time())
    hired = store.fill_open_seats(db, avs)
    assert hired and hired[0]["agent"]["id"] == explorer and hired[0]["woken"]
    woken = store.row("agents", int(explorer))
    assert woken["mode"] == "exploit" and woken["wake_after"] == 0 and woken["seat_id"] == writer["id"]
    assert "woken from exploration" in store.rows("personnel_log", db, order="id DESC", limit=1)[0]["reason"]


def test_persona_states_the_escalation_line_format(db):
    avs = templates.seed_company(db, "avs_studio")
    seats = {int(s["id"]): s for s in store.seats_for(db, avs)}
    writer = store.seat_by_key(db, avs, "writer_1")
    block = persona_block(store.row("agents", int(writer["agent_id"])), writer, store.row("companies", avs), seats)
    assert "ESCALATE: up :: " in block and cycles._ESCALATE_RE.search("ESCALATE: up :: who decides?")


# ----------------------------------------------------------------------------- GitHub revert safety

def test_revert_refuses_records_without_pre_push_state_or_an_unmerged_pull_request(monkeypatch):
    from tests.test_github_push import FakeResponse, make_fake

    calls = []
    monkeypatch.setattr(gp.requests, "request", make_fake(calls, existing=("docs/RUNBOOK.md", "bin/run")))
    writer = gp.GitHubWriter("ghp_secret_token_123", "me/proj")
    record = writer.push_files([("bin/run", "x"), ("new.txt", "y")], "deploy-kit/app-2", "m", "t", "b")
    assert record.modes == {"bin/run": "100644", "new.txt": "100644"} and record.previous["new.txt"] is None
    tree = next(j for m, p, j in calls if p.endswith("/git/trees") and m == "POST")
    assert {e["path"]: e["mode"] for e in tree["tree"]} == {"bin/run": "100644", "new.txt": "100644"}  # the base mode is kept
    bare = gp.PushRecord(owner="me", repo="proj", base_branch="main", base_sha="s", branch="b", commit_sha="c", pr_number=101, pr_url="", files=["bin/run"])
    with pytest.raises(gp.GitHubPushError, match="no pre-push state"):
        writer.open_revert(bare)
    with pytest.raises(gp.GitHubPushError, match="only a push"):
        writer.open_revert(gp.PushRecord(**{**bare.__dict__, "kind": "init"}))
    real = make_fake(calls)

    def unmerged(method, url, headers=None, json=None, timeout=None):
        if "/pulls/" in url and method == "GET":
            return FakeResponse(200, {"merged": False})
        return real(method, url, headers=headers, json=json, timeout=timeout)

    monkeypatch.setattr(gp.requests, "request", unmerged)
    with pytest.raises(gp.GitHubPushError, match="not merged"):
        writer.open_revert(record)


# ----------------------------------------------------------------------------- preview

def test_preview_script_runs_only_under_a_nonce():
    document = preview.safe_preview_document("<div><button>hi</button></div>")
    nonce = re.search(r"script-src 'nonce-([^']+)'", document).group(1)
    assert "'unsafe-inline'" not in document.split("script-src")[1].split(";")[0]
    assert f'<script nonce="{nonce}">' in document and document.count("<script") == 1
    assert nonce != re.search(r"script-src 'nonce-([^']+)'", preview.safe_preview_document("<p>x</p>")).group(1)


# ----------------------------------------------------------------------------- app

KEY_ENVS = ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.setenv("CHAT_JOHNSON_JOB_WORKERS", "0")
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delattr(st, "segmented_control", raising=False)
    return AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)


def test_launch_after_a_migration_stores_the_nodes_on_the_live_thread(app, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-key-for-the-launch-button")
    app.query_params["scope"] = "visitor-migrate"
    app.query_params["ws"] = "task_finder"
    app.run()
    thread = int(vault.active_thread("visitor-migrate", "task_finder")["id"])
    for index in range(330):  # over the window: the health sweep migrates before launch
        vault.append_message("visitor-migrate", "user" if index % 2 == 0 else "assistant", f"turn {index} " * 20, thread_id=thread, workspace="task_finder")
    app.run()
    app.chat_input[0].set_value("Research the history of tidal power").run()
    next(b for b in app.button if b.label.startswith("Launch workstreams")).click().run()
    assert not app.exception
    job = vault.list_jobs("visitor-migrate", kind="mission")[0]
    live = int(job["thread_id"])
    assert vault.mission_nodes_for(live) and (live == thread or vault.mission_nodes_for(thread) == [])


def test_invalid_scope_is_replaced_and_clear_all_keys_works(app):
    app.query_params["scope"] = "../../etc/passwd"
    app.run()
    assert not app.exception
    assert app.session_state["project_scope"].startswith("visitor-")
    app.session_state["byok_keys"] = {"GEMINI_API_KEY": "AIza-x"}
    app.run()
    clear = [b for b in app.button if "Clear all keys" in b.label]
    if clear:
        clear[0].click().run()
        assert not app.exception and app.session_state["byok_keys"] == {}


def test_deploy_kit_url_check_refuses_a_private_host(app):
    app.query_params["ws"] = "repository"
    app.run()
    assert not app.exception
    inputs = [t for t in app.text_input if "URL" in (t.label or "")]
    if inputs:
        inputs[0].input("http://127.0.0.1:8501/").run()
        buttons = [b for b in app.button if "Check" in b.label and "URL" in b.label]
        if buttons:
            buttons[0].click().run()
            assert not app.exception
            assert any("refused" in (e.value or "") for e in app.error) or any("refused" in (m.value or "") for m in app.markdown)
