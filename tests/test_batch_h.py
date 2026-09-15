"""Batch H: controlled chaos (pink-wave signal), long-distance memory, per-project briefs, per-company waves, editable roles."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import executor, pinkwave, router, vault  # noqa: E402
from orchestrator.prompting import build_prompt_messages  # noqa: E402
from orchestrator.router import CORTEX_ENDPOINTS, RouteDecision, _heavy_pipeline, select_milp_endpoint  # noqa: E402
from orchestrator.society import release, store, templates  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    pinkwave.deactivate()
    yield "scope-h"
    pinkwave.deactivate()


def counter(start=0):
    state = {"n": start}

    def step(feature):
        state["n"] += 1
        return state["n"]

    return step


# ----------------------------------------------------------------------------- pink wave

def test_profiles_are_reproducible_bounded_and_distinct():
    a = [pinkwave.signal("routing", i, "pink") for i in range(64)]
    assert a == [pinkwave.signal("routing", i, "pink") for i in range(64)]
    assert a != [pinkwave.signal("routing", i, "white") for i in range(64)]
    assert a != [pinkwave.signal("routing", i, "brown") for i in range(64)]
    assert all(0.0 <= pinkwave.unit("memory", i, p) <= 1.0 for p in pinkwave.PROFILES for i in range(200))
    assert pinkwave.signal("routing", 5) == pinkwave.signal("routing", 5 + pinkwave.LENGTH)  # the walk wraps


def test_settings_persist_per_scope_and_clamp(db):
    saved = pinkwave.save_settings(db, 1.7, {"routing": "brown", "bogus": "pink", "heavy": "nope"})
    assert saved.gain == 1.0 and saved.profiles["routing"] == "brown" and saved.profiles["heavy"] == "pink"
    assert pinkwave.settings_for(db).profiles["routing"] == "brown"
    assert pinkwave.settings_for("another-scope").gain == pinkwave.DEFAULT_GAIN  # untouched default
    chaos = pinkwave.Chaos(db)
    first, second = chaos.step("memory"), chaos.step("memory")
    assert second == first + 1 and pinkwave.Chaos("another-scope").step("memory") == 1  # counters are scoped
    assert chaos.preview()["memory"]["step"] == second


def test_gain_zero_switches_every_nudge_off(db):
    chaos = pinkwave.Chaos(db, pinkwave.ChaosSettings(gain=0.0), step_source=counter())
    assert chaos.routing_jitter(["a", "b"]) == {}
    assert chaos.heavy_schedule(0.2) is None
    assert chaos.recall_share() == pinkwave.RECALL_BASE_SHARE
    assert chaos.digest_recall_chars() == pinkwave.DIGEST_RECALL_BASE


def test_nudges_stay_inside_their_bounds_at_full_gain(db):
    chaos = pinkwave.Chaos(db, pinkwave.ChaosSettings(gain=1.0), step_source=counter())
    for _ in range(50):
        jitter = chaos.routing_jitter(["google_ai_studio", "groq", "huggingface"])
        assert set(jitter) == {"google_ai_studio", "groq", "huggingface"}
        assert all(0.0 <= v <= pinkwave.ROUTING_MAX_JITTER for v in jitter.values())
        draft, critique, synthesis = chaos.heavy_schedule(0.2)
        assert 0.2 <= draft <= 0.2 + pinkwave.HEAVY_DRAFT_SPREAD and critique == 0.0 and synthesis == 0.2
        assert pinkwave.RECALL_BASE_SHARE <= chaos.recall_share() <= pinkwave.RECALL_BASE_SHARE + pinkwave.RECALL_SPREAD
        assert pinkwave.DIGEST_RECALL_BASE <= chaos.digest_recall_chars() <= pinkwave.DIGEST_RECALL_BASE + pinkwave.DIGEST_RECALL_SPREAD
    assert len({v for v in (chaos.routing_jitter(["groq"])["groq"] for _ in range(20))}) > 1  # the walk moves


# ----------------------------------------------------------------------------- routing and Heavy Mode

def test_routing_jitter_never_moves_a_clear_winner_or_a_blocked_endpoint(db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    zero = {name: 0.0 for name in CORTEX_ENDPOINTS}
    pinkwave.activate(db, pinkwave.ChaosSettings(gain=1.0))
    for _ in range(12):
        decision = select_milp_endpoint("context_load", 2_000, entropy_by_endpoint=zero)
        assert decision.endpoint.name == "google_ai_studio" and "chaos=pink" in decision.reason
        chat = select_milp_endpoint("chat", 500, entropy_by_endpoint=zero)
        assert chat.endpoint.name == "groq"
    usage = {"groq": {"rpm_used": 30, "tpm_used": 0}}
    decision = select_milp_endpoint("chat", 500, entropy_by_endpoint=zero, current_usage=usage)
    assert decision.endpoint.name != "groq"  # the capacity rows still decide
    pinkwave.deactivate()
    plain = select_milp_endpoint("chat", 500, entropy_by_endpoint=zero)
    assert "chaos=" not in plain.reason


def test_heavy_pipeline_applies_the_temperature_schedule_only_when_given():
    seen = []

    def one_pass(task_type, messages, tokens, temperature=None):
        seen.append((task_type, temperature))
        return f"{task_type}-answer", RouteDecision("free", "m", task_type, "r")

    text, decision = _heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "q"}], 900, temperatures=(0.5, 0.0, 0.2))
    assert seen == [("chat", 0.5), ("reasoning", 0.0), ("chat", 0.2)] and "temperatures=0.5/0.0/0.2" in decision.reason
    seen.clear()
    _heavy_pipeline(one_pass, "chat", [{"role": "user", "content": "q"}], 900)
    assert seen == [("chat", None), ("reasoning", None), ("chat", None)]


def test_heavy_schedule_comes_from_the_active_wave(db):
    pinkwave.activate(db, pinkwave.ChaosSettings(gain=0.5))
    schedule = router._heavy_schedule(0.2)
    assert schedule is not None and schedule[1] == 0.0 and 0.2 <= schedule[0] <= 0.4 and schedule[2] == 0.2
    pinkwave.deactivate()
    assert router._heavy_schedule(0.2) is None


# ----------------------------------------------------------------------------- long-distance memory

def seed_summary(scope, thread_id, content):
    with vault._open_database() as connection:
        connection.execute(
            "INSERT INTO summaries (project_scope, covers_from_id, covers_to_id, message_count, content, method, created_at, thread_id) VALUES (?, 1, 2, 2, ?, 'extractive', ?, ?)",
            (scope, content, time.time(), int(thread_id)),
        )


def test_recall_pulls_matching_lines_from_other_chats_only_inside_the_scope(db):
    a = vault.create_thread(db, "Postgres planning", workspace="normal_chat")
    b = vault.create_thread(db, "Today", workspace="task_finder")
    seed_summary(db, a, "Decided: the postgres migration runs on Friday with pgbouncer in front.\nUnrelated line about lunch.")
    other = vault.create_thread("scope-other", "Leak", workspace="normal_chat")
    seed_summary("scope-other", other, "Secret other-scope postgres migration detail.")
    vault.set_thread_mission(b, "Ship the postgres migration runbook", db)
    recalled = vault.recall_memory(db, "when is the postgres migration?", b, 2_000)
    assert recalled.startswith(vault.RECALL_PREFIX) and "pgbouncer" in recalled and 'chat "Postgres planning"' in recalled
    assert "lunch" not in recalled and "other-scope" not in recalled and "mission of chat" not in recalled  # own mission excluded, other scope never
    from_a = vault.recall_memory(db, "when is the postgres migration?", a, 2_000)
    assert "mission of chat \"Today\"" in from_a and "pgbouncer" not in from_a  # a chat's own newest summaries are live context, not recall
    assert vault.recall_memory(db, "", b, 2_000) == ""
    tiny = vault.recall_memory(db, "postgres migration", b, 120)
    assert len(tiny) <= 120


def test_context_and_prompt_carry_recall_within_the_budget(db):
    a = vault.create_thread(db, "Earlier", workspace="normal_chat")
    b = vault.create_thread(db, "Now", workspace="normal_chat")
    seed_summary(db, a, "Constraint: the cache TTL stays at 300 seconds for the checkout service.")
    parts = vault.context_parts(db, 4_000, thread_id=b, recall_query="what cache TTL does checkout use", recall_share=0.2)
    assert "cache TTL" in parts["memory"] and parts["recall"] and len(parts["recall"]) <= 800
    assert vault.context_parts(db, 4_000, thread_id=b)["recall"] == ""
    messages = build_prompt_messages(db, "remind me of the checkout cache TTL", workspace="normal_chat", thread_id=b)
    assert vault.RECALL_PREFIX in messages[0]["content"]
    silent = build_prompt_messages(db, "remind me of the checkout cache TTL", workspace="normal_chat", thread_id=b, recall_share=0)
    assert vault.RECALL_PREFIX not in silent[0]["content"]


def test_vision_digest_carries_long_distance_memory(db):
    a = vault.create_thread(db, "Design", workspace="normal_chat")
    b = vault.create_thread(db, "Build", workspace="normal_chat")
    seed_summary(db, a, "Decision: the deploy kit targets oracle-vm with caddy basic auth.")
    for i in range(5):
        vault.append_message(db, "user", f"We must build the deploy kit for the oracle-vm target, step {i}.", thread_id=b)
        vault.append_message(db, "assistant", "Working on the deploy kit.", thread_id=b)
    digest = vault.build_vision_digest(db, b)
    assert "## Long-distance memory" in digest and "caddy basic auth" in digest
    assert "## Long-distance memory" not in vault.build_vision_digest(db, b, recall_characters=0)


def test_task_memory_path_is_private_per_scope():
    assert executor.memory_path_for(".orchestrator/memory.json") == ".orchestrator/memory.json"
    one, two = executor.memory_path_for(".orchestrator/memory.json", "visitor-a"), executor.memory_path_for(".orchestrator/memory.json", "visitor-b")
    assert one != two and one.startswith(".orchestrator/memory-") and one.endswith(".json")


# ----------------------------------------------------------------------------- society: briefs, waves, roles

def test_product_briefs_are_per_project_and_rewrite_open_items(db):
    sw = templates.seed_company(db, "software_co")
    other = templates.seed_company("scope-h2", "software_co")
    cat = next(c for c in store.catalog_for(db, sw) if c["key"] == "api")
    assert "Price idea" in cat["brief"]
    items = store.rows("work_items", db, "catalog_id = ?", (int(cat["id"]),))
    assert all(templates.PRODUCT_HEADER_END in i["brief"] and "Product brief:" in i["brief"] for i in items)
    store.set_work_status(int(items[0]["id"]), "done")
    rewritten = templates.set_product_brief(db, int(cat["id"]), title="The Studio API", brief="Buyer: platform teams. Promise: one key.")
    assert rewritten == len(items) - 1
    fresh = store.row("catalog", int(cat["id"]), db)
    assert fresh["title"] == "The Studio API" and fresh["brief"].startswith("Buyer: platform teams")
    open_items = store.rows("work_items", db, "catalog_id = ? AND status != 'done'", (int(cat["id"]),))
    assert all(i["brief"].startswith("Product: The Studio API.") and "platform teams" in i["brief"] and i["brief"].endswith(templates.split_item_brief(i["brief"])[1]) for i in open_items)
    assert "Price idea" in store.row("work_items", int(items[0]["id"]), db)["brief"]  # the finished item keeps its history
    twin = next(c for c in store.catalog_for("scope-h2", other) if c["key"] == "api")
    assert twin["title"] == "The API" and "Price idea" in twin["brief"]  # another project keeps its own brief
    templates.add_product(db, sw, "widget", "Widget", "does widgets", brief="Buyer: everyone.")
    widget = next(c for c in store.catalog_for(db, sw) if c["key"] == "widget")
    assert widget["brief"] == "Buyer: everyone." and all("Buyer: everyone." in i["brief"] for i in store.rows("work_items", db, "catalog_id = ?", (int(widget["id"]),)))


def finish(scope, company_id, catalog_id, title):
    artifact_id, _ = vault.save_artifact(scope, f"{title}.md", f"x/{catalog_id}-{title}.md", f"# {title}\nbody", "markdown")
    item_id = store.add_work_item(scope, company_id, f"{title} · {catalog_id}", "brief", catalog_id=int(catalog_id))
    store.set_work_status(item_id, "done", artifact_id=int(artifact_id))
    store.update("catalog", int(catalog_id), stage="final")


def test_wave_size_is_per_company_and_the_next_wave_opens_after_a_release(db):
    avs = templates.seed_company(db, "avs_studio", wave_size=3)
    company = store.row("companies", avs, db)
    assert company["wave_size"] == 3 and sum(1 for c in store.catalog_for(db, avs) if c["release_wave"] == 1) == 3
    assert release.current_wave(db, avs) == 1
    status = release.wave_status(db, avs)
    assert status["wave"] == 1 and status["size"] == 3 and status["needed"] == 3 and not status["gate_met"]
    wave_one = release.wave_works(db, avs, 1)
    for cat in wave_one:
        finish(db, avs, int(cat["id"]), "Story bible")
    assert release.wave_status(db, avs)["gate_met"]
    store.update("companies", avs, wave_size=5)  # a size above the wave's work count needs every work in the wave, no more
    assert release.wave_status(db, avs)["needed"] == 3 and release.wave_status(db, avs)["gate_met"]
    store.update("catalog", int(wave_one[0]["id"]), stage="edit")
    assert not release.wave_status(db, avs)["gate_met"] and release.assemble_wave(db, avs) is None
    store.update("catalog", int(wave_one[0]["id"]), stage="final")
    store.update("companies", avs, wave_size=3)
    release_id = release.assemble_wave(db, avs)
    assert release_id and store.row("releases", release_id)["wave"] == 1
    backlog_before = store.count("work_items", db, "company_id = ? AND status = 'backlog'", (avs,))
    published = release.approve_release(db, release_id, "Ship.")
    assert len(published) == 3 and release.current_wave(db, avs) == 2
    wave_two = release.wave_works(db, avs, 2)
    assert wave_two and all(c["stage"] == "development" for c in wave_two)
    assert store.count("work_items", db, "company_id = ? AND status = 'backlog'", (avs,)) == backlog_before + 2 * len(wave_two) + 5 * 3
    assert release.wave_status(db, avs)["wave"] == 2 and release.wave_status(db, avs)["release"] is None


def test_a_software_company_releases_all_its_products_together(db):
    sw = templates.seed_company(db, "software_co")
    status = release.wave_status(db, sw)
    assert status["size"] == 3 and status["needed"] == 3 and status["works"] == 3
    for cat in store.catalog_for(db, sw):
        finish(db, sw, int(cat["id"]), "Positioning")
    release_id = release.assemble_wave(db, sw)
    assert release_id and len(store.load_json(store.row("releases", release_id)["works"], [])) == 3
    assert len(release.approve_release(db, release_id)) == 3
    assert all(c["stage"] == "published" for c in store.catalog_for(db, sw))


def test_roles_parse_edit_and_new_seats_get_filled(db):
    assert store.parse_roles("write; edit, review;write") == ["write", "edit", "review"]
    assert store.parse_roles('["a", "b"]') == ["a", "b"]
    assert store.parse_kpis("deliverables=1; review_pass_rate = 0.6; junk; reviews: 2") == {"deliverables": 1.0, "review_pass_rate": 0.6, "reviews": 2.0}
    assert store.parse_kpis('{"reports": 1}') == {"reports": 1.0} and store.format_kpis({"reports": 1.0, "reviews": 2.5}) == "reports=1; reviews=2.5"
    avs = templates.seed_company(db, "avs_studio")
    templates.seed_academy(db, 20)
    ceo = store.seat_by_key(db, avs, "ceo")
    store.update("seats", int(ceo["id"]), db, title="Chief Story Officer", roles=store.parse_roles("set the vision; approve every brief"), kpis=store.parse_kpis("reports=1"))
    edited = store.row("seats", int(ceo["id"]), db)
    assert edited["title"] == "Chief Story Officer" and store.load_json(edited["roles"], []) == ["set the vision", "approve every brief"]
    dept = next(d for d in store.departments_for(db, avs) if d["key"] == "editorial")
    seat_id = store.add_seat(db, avs, "Continuity Editor", int(dept["id"]), int(ceo["id"]), "continuity; canon; timeline checks", "reviews=1", 4)
    twin = store.add_seat(db, avs, "Continuity Editor", int(dept["id"]), int(ceo["id"]), "", "", 9)
    seat, second = store.row("seats", seat_id, db), store.row("seats", twin, db)
    assert seat["key"] == "continuity_editor" and second["key"] == "continuity_editor_2" and second["importance"] == 5
    assert store.load_json(seat["kpis"], {}) == {"reviews": 1.0} and store.load_json(second["roles"], []) == ["do the work the board describes"]
    for name in ("Grad A", "Grad B"):
        store.add_agent(db, name, "a free graduate", tier="philosopher")
    hired = store.fill_open_seats(db, avs)
    assert {int(h["seat"]["id"]) for h in hired} >= {seat_id, twin} and store.row("seats", seat_id, db)["status"] == "filled"
    with pytest.raises(ValueError):
        store.add_seat(db, avs, "   ", None, None, "")
