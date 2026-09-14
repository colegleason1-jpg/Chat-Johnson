"""Batch D: the layout solver, the canvas preview, web QA, and the executor steps in missions."""
import http.server
import json
import os
import sys
import threading
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import jobs, mission_runner, missions, spatial, vault, webqa  # noqa: E402
from orchestrator.router import RouteDecision  # noqa: E402
from orchestrator.spatial_preview import CSP, scene_preview_document  # noqa: E402


def random_spec(seed: int, count: int = 12) -> spatial.SceneSpec:
    rng = np.random.default_rng(seed)
    room = (8.0, 6.0, 3.0)
    objects = []
    for i in range(count):
        size = tuple(float(v) for v in rng.uniform(0.3, 1.5, size=3))
        anchor = ("floor", "wall", "free")[i % 3]
        position = tuple(float(v) for v in rng.uniform(0, 2, size=3)) if i % 2 else None
        objects.append(spatial.SceneObject(name=f"o{i}", size=size, mass=float(rng.uniform(1, 50)), anchor=anchor, position=position))
    return spatial.SceneSpec(room=room, objects=objects)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_solver_leaves_no_overlaps_keeps_floor_objects_down_and_everything_inside(seed):
    placed = spatial.solve_layout(random_spec(seed))
    assert placed.report["overlaps_after"] == 0 and placed.report["inside_room"] and placed.report["resolved"]
    for o in placed.objects:
        if o.anchor in ("floor", "wall"):
            assert o.position[2] == 0.0
        for axis in range(3):
            assert -1e-6 <= o.position[axis] and o.position[axis] + o.size[axis] <= placed.room[axis] + 1e-6
    again = spatial.solve_layout(random_spec(seed))
    assert [o.position for o in again.objects] == [o.position for o in placed.objects]  # deterministic


def test_the_lighter_box_moves_more_and_walls_snap():
    spec = spatial.parse_scene({
        "room": {"width": 10, "depth": 10, "height": 3},
        "objects": [
            {"name": "heavy", "size": [2, 2, 1], "mass": 100, "position": [4, 4, 0]},
            {"name": "light", "size": [2, 2, 1], "mass": 1, "position": [4.5, 4, 0]},
            {"name": "shelf", "size": [1, 0.4, 2], "mass": 20, "anchor": "wall", "position": [1, 4, 0]},
        ],
    })
    placed = spatial.solve_layout(spec)
    by_name = {o.name: o for o in placed.objects}
    assert placed.report["overlaps_after"] == 0 and by_name["light"].moved > by_name["heavy"].moved
    assert by_name["shelf"].position[0] == 0.0  # nearest wall was x = 0
    assert "| heavy |" in spatial.scene_markdown(placed) and json.loads(spatial.scene_json(placed))["report"]["resolved"]
    assert json.loads(spatial.scene_json(placed))["objects"][0]["position"][0] == round(by_name["heavy"].position[0], 4)


def test_scene_parser_accepts_a_block_and_rejects_bad_specs():
    text = 'Intent first.\n```scene\n{"room": [4, 4, 3], "objects": [{"name": "table", "size": [1, 1, 0.8]}]}\n```\n'
    spec = spatial.parse_scene_block(text)
    assert spec and spec.objects[0].name == "table" and spec.objects[0].anchor == "floor"
    assert spatial.parse_scene_block("no block here") is None
    with pytest.raises(spatial.SceneError):
        spatial.parse_scene_block("```scene\nnot json\n```")
    for bad in (
        {"room": [4, 4, 3]},
        {"room": [4, 4, 3], "objects": [{"size": [-1, 1, 1]}]},
        {"room": [4, 4, 3], "objects": [{"size": [9, 1, 1]}]},
        {"room": [4, 4, 3], "objects": [{"size": [1, 1, 1], "anchor": "ceiling"}]},
        {"room": [4, 4, 3], "objects": [{"size": [1, 1, 1]}] * (spatial.MAX_OBJECTS + 1)},
    ):
        with pytest.raises(spatial.SceneError):
            spatial.parse_scene(bad)


def test_preview_document_is_self_contained_and_escaped():
    placed = spatial.solve_layout(spatial.parse_scene({"room": [5, 5, 3], "objects": [{"name": "<b>desk</b>", "size": [1, 1, 1]}]}))
    doc = scene_preview_document(spatial.scene_dict(placed))
    assert CSP in doc and "<canvas" in doc and "src=" not in doc and "cdnjs" not in doc
    assert "&lt;b&gt;desk&lt;/b&gt;" in doc and "<b>desk</b>" not in doc
    assert "drag to rotate" in doc and "resolved" in doc


def test_mission_kinds_carry_executor_steps():
    plan = missions.task_plan("Arrange the furniture in a 6 by 5 metre room layout", 3)
    assert plan[0]["kind"] == "spatial" and [s.get("executor") for s in plan] == [None, "solver", None]
    plan = missions.task_plan("Check the site https://example.com/?health=1 is up", 2)
    assert plan[0]["kind"] == "webcheck" and plan[0]["executor"] == "webqa" and "executor" not in plan[1]
    assert missions.classify_mission("write a 2 page essay about rooms") == "writing"  # a room in prose is still writing


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            body = json.dumps({"status": "ok", "build": "abc1234"}).encode()
        elif self.path.startswith("/missing"):
            self.send_response(404)
            self.end_headers()
            return
        else:
            body = b"<html><body>Chat Johnson Master Studio</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return None


@pytest.fixture()
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_check_url_reports_status_text_and_health(server):
    result = webqa.check_url(server + "/", expect_text="Master Studio")
    assert result["ok"] and result["status"] == 200 and result["text_found"] is True and result["health"] is None and result["elapsed_ms"] >= 0
    assert webqa.check_url(server + "/health").get("health") == {"status": "ok", "build": "abc1234"}
    assert not webqa.check_url(server + "/missing")["ok"] and webqa.check_url(server + "/", expect_text="nope")["text_found"] is False
    down = webqa.check_url("http://127.0.0.1:9/")
    assert not down["ok"] and down["error"]
    assert webqa.first_url("see https://a.example/x, then") == "https://a.example/x" and webqa.first_url("none") == ""
    assert "| status | 200 |" in webqa.check_markdown(result, {"available": False, "error": "no browser"}) and "unavailable" in webqa.check_markdown(result, {"available": False, "error": "no browser"})


def test_browser_check_is_honest_without_playwright(monkeypatch):
    monkeypatch.setattr(webqa, "browser_available", lambda: False)
    result = webqa.browser_check("http://127.0.0.1:9/", [])
    assert result["available"] is False and not result["ok"] and "VM worker" in result["error"]


def test_spatial_mission_runs_the_solver_step_and_saves_the_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()

    def fake_generate(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        prompt = messages[-1]["content"]
        if "scene JSON block" in prompt:
            text = 'A study for two.\n```scene\n{"room": {"width": 5, "depth": 4, "height": 3}, "objects": [{"name": "desk", "size": [1.6, 0.8, 0.75], "mass": 40, "anchor": "wall"}, {"name": "chair", "size": [0.6, 0.6, 1], "mass": 6, "position": [0.2, 0.2, 0]}]}\n```'
        else:
            text = "Walkthrough: the desk sits against the wall and the chair was moved clear of it."
        return text, RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setattr(mission_runner, "generate_mode", fake_generate)
    monkeypatch.setattr(mission_runner, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    thread = int(vault.active_thread("s", "task_finder")["id"])
    plan = missions.task_plan("Arrange a small study room layout for two people", 3)
    job_id = jobs.enqueue("s", mission_runner.KIND, {"goal": "Arrange a small study room layout", "plan": plan, "mode": "normal", "max_tokens": 600}, {}, thread_id=thread)
    jobs.run_job(vault.claim_job("t", (mission_runner.KIND,)))
    view = vault.job_view(vault.job_by_id(job_id))
    assert view["status"] == "done", view
    result = view["result"]
    assert result["succeeded"] == 3 and result["failed"] == 0 and result["scene_artifact"] and result["scene_report"]["resolved"]
    scene = json.loads(vault.export_artifact(int(result["scene_artifact"]))[1])
    assert {o["name"] for o in scene["objects"]} == {"desk", "chair"} and scene["report"]["overlaps_after"] == 0
    rows = vault.recent_messages("s", 20, thread_id=thread)
    assert any(r["provider"] == "local-executor/solver" and "Resolved layout" in r["content"] for r in rows)
    assert time.time() > 0
    # A solver step with no scene block fails that step with a plain reason and the mission continues.
    monkeypatch.setattr(mission_runner, "generate_mode", lambda *a, **k: ("no block", RouteDecision("fake", "m", "chat", "r")))
    job_id = jobs.enqueue("s", mission_runner.KIND, {"goal": "Arrange a room layout", "plan": missions.task_plan("Arrange a room layout", 3), "mode": "normal", "max_tokens": 600}, {}, thread_id=thread)
    jobs.run_job(vault.claim_job("t", (mission_runner.KIND,)))
    result = vault.job_view(vault.job_by_id(job_id))["result"]
    assert result["failed"] == 1 and "scene block" in result["failures"][0][1] and "scene_artifact" not in result
