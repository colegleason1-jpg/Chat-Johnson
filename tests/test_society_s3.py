"""S3: leisure inquiries, the dream bank, the society tick, the release loop, escalations, and feedback themes."""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import jobs, vault  # noqa: E402
from orchestrator.router import RouteDecision  # noqa: E402
from orchestrator.society import academy, cycles, economy, inquiries, leisure, release, store, templates, tick  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    monkeypatch.setattr(economy.Treasury, "daily_remaining", lambda self: 10_000_000)
    yield "scope-3"


class FakeResp:
    def __init__(self, payload=None, text=""):
        self._payload, self.text, self.status_code = payload, text, 200

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        return None


def test_inquiry_sources_parse_their_apis(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if "list=search" in url:
            return FakeResp({"query": {"search": [{"title": "Tide"}]}})
        if "page/summary" in url:
            return FakeResp({"title": "Tide", "extract": "Tides are the rise and fall of sea levels.", "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Tide"}}})
        if "gutendex" in url:
            return FakeResp({"results": [{"title": "Moby Dick", "authors": [{"name": "Melville"}], "formats": {"text/plain; charset=utf-8": "https://gutenberg.org/x.txt"}}]})
        if url.endswith("x.txt"):
            return FakeResp(text="header\n*** START OF THIS PROJECT GUTENBERG EBOOK ***\n" + "Call me Ishmael. " * 3)
        if "arxiv" in url:
            return FakeResp(text="<feed><entry><id>http://arxiv.org/abs/1</id><title>Tidal locking</title><summary>A study of tides.</summary></entry></feed>")
        if "openlibrary" in url:
            return FakeResp({"docs": [{"key": "/works/OL1W", "title": "The Sea", "first_publish_year": 1950, "author_name": ["A"], "subject": ["ocean"]}]})
        if "algolia" in url:
            return FakeResp({"hits": [{"title": "Tides explained", "points": 42, "url": "https://example.com/t"}]})
        if "custom.example" in url:
            assert headers.get("X-Key") == "sekret"
            return FakeResp({"answer": 42})
        raise AssertionError(url)

    monkeypatch.setattr(inquiries.requests, "get", fake_get)
    url, text = inquiries.inquire("wikipedia", "tide")
    assert url.endswith("/Tide") and "rise and fall" in text
    url, text = inquiries.inquire("gutenberg", "moby")
    assert url.endswith("x.txt") and text.startswith("Moby Dick by Melville.") and "Ishmael" in text and "header" not in text
    assert "Tidal locking: A study of tides." in inquiries.inquire("arxiv", "tides")[1]
    assert "The Sea (1950) by A; subjects: ocean" in inquiries.inquire("openlibrary", "sea")[1]
    assert "Tides explained (42 points)" in inquiries.inquire("hackernews", "tides")[1]
    from orchestrator.config import bind_session_keys
    bind_session_keys({"CUSTOM_KEY": "sekret"})
    try:
        url, text = inquiries.inquire("mine", "q", [{"name": "mine", "url": "https://custom.example/api?q={query}", "headers": {"X-Key": "env:CUSTOM_KEY"}}])
    finally:
        bind_session_keys({})
    assert "answer" in text and url.endswith("q=q")
    with pytest.raises(KeyError):
        inquiries.inquire("nope", "q")


def fake_call_free(calls):
    def call_free(ctx, budget, cycle_id, agent, prompt, task_type, max_tokens, mode="normal", role_note="", **kwargs):
        calls.append(prompt[:30])
        if "off the clock" in prompt:
            text = "SOURCE: wikipedia\nQUERY: tidal rhythms in fiction\nWAKE_HOURS: 2"
        else:
            text = "Notes: tides follow the moon; surprising: spring tides; connects to my drafting focus on rhythm. " * 3
        budget.step(250)
        return text, RouteDecision("fake", "m", task_type, "r")
    return call_free


class Ctx:
    def __init__(self, scope):
        self.project_scope, self.job_id = scope, 1
    def check_cancel(self):
        return None
    def progress(self, **fields):
        return None


def test_leisure_spends_balance_writes_the_dream_bank_and_sets_the_next_wake(db, monkeypatch):
    rich = store.add_agent(db, "Producer 001", "p", tier="producer", allowance=500, focus="drafting")
    poor = store.add_agent(db, "Producer 002", "p", tier="producer", allowance=500)
    asleep = store.add_agent(db, "Producer 003", "p", tier="producer", allowance=500, wake_after=time.time() + 3600)
    for agent in (rich, asleep):
        store.ledger_add(db, agent, "earn", 1000, "work")
    store.ledger_add(db, poor, "earn", 100, "work")
    monkeypatch.setattr(leisure, "inquire", lambda source, query, custom: ("https://en.wikipedia.org/wiki/Tide", "Tides are the rise and fall of sea levels caused by the moon."))
    calls = []
    budget = cycles.CycleBudget(8, 12_000)
    done = leisure.run_leisure(Ctx(db), budget, 0, fake_call_free(calls), cap=3)
    assert [d["agent"] for d in done] == ["Producer 001"]  # the poor one cannot afford it, the third is asleep
    entry = done[0]
    assert entry["source"] == "wikipedia" and entry["query"] == "tidal rhythms in fiction" and entry["status"] == "done" and entry["wake_hours"] == 2.0
    agent = store.row("agents", rich)
    assert agent["interest"] == "tidal rhythms in fiction" and agent["mode"] == "explore" and agent["wake_after"] > time.time() + 7000
    assert agent["balance"] == 1000 - entry["spent"] and entry["spent"] == leisure.LEISURE_COST
    bank = store.rows("dream_bank", db)
    assert len(bank) == 1 and "spring tides" in bank[0]["findings"] and "tidal" in bank[0]["tags"]
    assert store.rows("inquiries", db)[0]["status"] == "done"
    excerpt = leisure.dream_excerpt_for(db, rich, "draft a chapter about tides and the moon")
    assert excerpt.startswith("- [wikipedia] tidal rhythms in fiction:") and len(excerpt) <= leisure.DREAM_CHARS
    assert leisure.dream_excerpt_for(db, poor, "anything") == ""
    monkeypatch.setattr(leisure, "inquire", lambda *a: (_ for _ in ()).throw(RuntimeError("blocked")))
    store.update("agents", rich, wake_after=0)
    done = leisure.run_leisure(Ctx(db), cycles.CycleBudget(8, 12_000), 0, fake_call_free(calls), cap=3)
    assert done[0]["status"].startswith("error") and store.row("agents", rich)["balance"] == 1000 - 2 * leisure.LEISURE_COST


def test_society_tick_queues_due_cycles_runs_leisure_and_chains(db, monkeypatch):
    avs = templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 25)
    monkeypatch.setattr(academy, "generate_mode", lambda *a, **k: ("SOURCE: arxiv\nQUERY: tides\nWAKE_HOURS: 1", RouteDecision("fake", "m", "quick_text", "r")))
    monkeypatch.setattr(academy, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    monkeypatch.setattr(leisure, "inquire", lambda source, query, custom: ("https://arxiv.org/abs/1", "Tides. " * 50))
    for agent in store.agents_for(db)[:2]:
        store.ledger_add(db, int(agent["id"]), "earn", 1000, "work")
    job_id = tick.start_tick(db, {"GEMINI_API_KEY": "AIza-fake"}, interval_s=600, leisure_cap=2)
    assert tick.tick_state(db)["running"]
    jobs.run_job(vault.claim_job("t", (tick.KIND_TICK,)))
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["status"] == "done", view
    result = view["result"]
    assert result["queued"] == ["AVS Studio", "academy"] and len(result["explored"]) == 2 and all(e["status"] == "done" for e in result["explored"])
    assert len(vault.list_jobs(db, ("queued",), kind=cycles.KIND_COMPANY)) == 1 and len(vault.list_jobs(db, ("queued",), kind=academy.KIND_ACADEMY)) == 1
    successor = vault.list_jobs(db, ("queued",), kind=tick.KIND_TICK)
    assert len(successor) == 1 and vault.job_view(successor[0])["run_after"] > time.time() + 500
    assert store.count("dream_bank", db) == 2 and [c["kind"] for c in store.rows("cycles", db)] == ["leisure", "tick"]
    # A second tick right away queues nothing new: cycles are already queued and intervals have not passed.
    with vault._open_database() as connection:
        connection.execute("UPDATE jobs SET run_after = 0 WHERE kind = ?", (tick.KIND_TICK,))
    jobs.run_job(vault.claim_job("t", (tick.KIND_TICK,)))
    second = vault.job_view(vault.list_jobs(db, ("done",), kind=tick.KIND_TICK)[0])
    assert second["result"]["queued"] == []
    assert tick.stop_tick(db) == 1 and not tick.tick_state(db)["running"]
    assert "AIza-fake" not in json.dumps(view)
    _ = avs


def test_release_loop_final_edit_publish_and_go_to_market(db):
    avs = templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 30)
    cat = store.catalog_for(db, avs)[0]
    item = store.work_items_for(db, avs)[0]
    artifact_id, _ = vault.save_artifact(db, "draft.md", "company/avs_studio/draft.md", "# Draft\n\nThe tides answered.\n", "markdown")
    store.set_work_status(int(item["id"]), "board", artifact_id=int(artifact_id))
    store.update("catalog", int(cat["id"]), stage="preliminary_review")
    first = release.request_final_edit(db, avs, int(cat["id"]), "Tighten the opening; the keeper needs a name.")
    assert first == release.request_final_edit(db, avs, int(cat["id"]), "again")  # one open final-edit item per work
    final_item = store.row("work_items", first)
    assert final_item["importance"] == 5 and "keeper needs a name" in final_item["brief"] and final_item["status"] == "backlog"
    assert {d["key"] for d in store.departments_for(db, avs) if not d["active"]} == {"marketing", "sales"}
    before = len(store.work_items_for(db, avs))
    pool_before = len(store.graduate_pool(db))
    assert 0 < pool_before < 4  # a 30-agent society holds a couple of free Philosophers: not enough for all four opened seats
    published = release.publish_work(db, int(cat["id"]))
    cat = store.row("catalog", int(cat["id"]))
    assert cat["stage"] == "published" and cat["artifact_id"] == published
    assert "tides answered" in vault.export_artifact(published)[1]
    assert any(a["file_path"] == "company/avs_studio/published/ip-project-01-title-to-be-set-by-the-boa.md" for a in vault.recent_artifacts(db, 10))
    assert all(d["active"] for d in store.departments_for(db, avs))
    assert len(store.work_items_for(db, avs)) == before + len(release.GO_TO_MARKET_ITEMS)
    marketing = [s for s in store.seats_for(db, avs) if s["key"] in ("marketing_lead", "copywriter", "sales_lead", "outreach_writer")]
    assert sum(1 for s in marketing if s["status"] == "filled") == pool_before  # every free graduate took an opened seat
    assert store.graduate_pool(db) == []
    assert "open_department" in [r["event"] for r in store.rows("personnel_log", db)]


def escalating_model(calls):
    def generate_mode(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        prompt = messages[-1]["content"]
        calls.append(prompt[:40])
        if "Level 10 meeting" in prompt:
            text = "HEADLINE: ok"
        elif "Rate each backlog" in prompt:
            import re
            text = "\n".join(f"#{i}: 4" for i in re.findall(r"#(\d+) ·", prompt))
        elif "Assign each item" in prompt:
            import re
            text = "\n".join(f"#{i} -> writer_1" for i in re.findall(r"#(\d+) ·", prompt))
        elif "Review the deliverable" in prompt:
            text = "PASS\nfine"
        elif "skip-level rule" in prompt:
            text = "Decision: name the keeper Mara; proceed."
        elif "Extract 3" in prompt:
            text = "- voice\n- pacing\n- setting"
        elif "report for the board" in prompt:
            text = "Report."
        else:
            text = "story bible premise world characters tone outline chapter questions board " * 12 + "\nESCALATE: up :: The outline conflicts with the timeline; who decides?"
        return text, RouteDecision("fake", "m", task_type, "r")
    return generate_mode


def test_escalations_route_by_the_skip_level_rule_and_get_answered(db, monkeypatch):
    avs = templates.seed_company(db, "avs_studio")
    release.record_board_feedback(db, avs, None, "The voice wanders and the pacing sags in the middle.")
    calls = []
    monkeypatch.setattr(cycles, "generate_mode", escalating_model(calls))
    monkeypatch.setattr(cycles, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    job_id = cycles.run_now(db, avs, {"GEMINI_API_KEY": "AIza-fake"}, call_tokens=300, max_calls=40)
    jobs.run_job(vault.claim_job("t", (cycles.KIND_COMPANY,)))
    result = vault.job_view(vault.job_by_id(job_id))["result"]
    assert result["status"] == "done", result
    escalations = store.rows("escalations", db)
    assert escalations, result["log"]
    seats = {int(s["id"]): s for s in store.seats_for(db, avs)}
    writer = store.seat_by_key(db, avs, "writer_1")
    first = escalations[0]
    assert first["from_seat"] == writer["id"] and seats[int(first["to_seat"])]["key"] == "production_lead"  # writer → lead writer → head of production
    assert cycles.skip_level_target(store.seat_by_key(db, avs, "production_lead"), seats, "down")["key"] in ("writer_1", "writer_2")
    answered = [e for e in escalations if e["status"] == "answered"]
    assert answered and "Mara" in answered[0]["reply"]
    assert next(e for e in result["log"] if e["step"] == "escalations")["answered"] >= 1
    writer_thread = store.row("seats", writer["id"])["thread_id"]
    assert any("REPLY TO YOUR ESCALATION" in m["content"] for m in vault.recent_messages(db, 50, thread_id=int(writer_thread)))
    themed = store.rows("feedback", db)[0]
    assert store.load_json(themed["themes"], []) == ["voice", "pacing", "setting"]
    assert next(e for e in result["log"] if e["step"] == "feedback")["themed"] == 1
