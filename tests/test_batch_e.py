"""Batch E: mission nodes (connectors, sub-missions, inputs, outputs, failure policies), the MCP client, Heavy streaming."""
import json
import os
import sys
from typing import List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import connectors_nodes, jobs, mcp_client, mission_runner, missions, router, vault  # noqa: E402
from orchestrator.router import ProviderError, RouteDecision  # noqa: E402

FAKE_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def fake_server(name="fake", env=None):
    return {"name": name, "command": sys.executable, "args": [FAKE_SERVER], "env": env or {}, "description": "test server"}


# ----------------------------------------------------------------------------- MCP client

def test_mcp_session_initializes_lists_and_calls_tools_and_redacts_errors(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hunter2-hunter2")
    with mcp_client.MCPSession(fake_server(env={"FAKE_MCP_SECRET": "env:MY_SECRET"}), timeout=10) as session:
        assert session.server_info["serverInfo"]["name"] == "fake"
        assert [t["name"] for t in session.list_tools()] == ["echo", "add", "secret"]
        assert mcp_client.result_text(session.call_tool("echo", {"text": "hi"})) == "hi"
        assert mcp_client.result_text(session.call_tool("add", {"a": 2, "b": 3})) == "5.0"
        assert mcp_client.result_text(session.call_tool("secret")) == str(len("hunter2-hunter2"))
        with pytest.raises(mcp_client.MCPError) as excinfo:
            session.call_tool("nope")
        assert "unknown tool" in str(excinfo.value) and "ghp_abcdefghijklmnopqrstuvwxyz" not in str(excinfo.value)


def test_mcp_session_times_out_and_reports_exits():
    with mcp_client.MCPSession(fake_server(), timeout=1) as session:
        with pytest.raises(mcp_client.MCPError) as excinfo:
            session.call_tool("slow")
        assert "did not answer" in str(excinfo.value)
    with pytest.raises(mcp_client.MCPError) as excinfo:
        mcp_client.MCPSession({"name": "missing", "command": "/no/such/binary-xyz", "args": []}, timeout=2)
    assert "cannot start" in str(excinfo.value)


def test_load_servers_and_call_by_name(tmp_path, monkeypatch):
    path = tmp_path / "mcp_servers.yaml"
    path.write_text(json.dumps({"servers": [{"name": "fake", "command": sys.executable, "args": [FAKE_SERVER], "env": {"FAKE_MCP_SECRET": "literal"}}, {"command": "no-name"}]}))
    monkeypatch.setenv("CHAT_JOHNSON_MCP_SERVERS", str(path))
    servers = mcp_client.load_servers()
    assert [s["name"] for s in servers] == ["fake"] and servers[0]["env"] == {"FAKE_MCP_SECRET": "literal"}
    result = mcp_client.call("fake", "echo", {"text": "yo"})
    assert result["text"] == "yo" and not result["is_error"]
    with pytest.raises(mcp_client.MCPError):
        mcp_client.call("other", "echo", {})
    probe = mcp_client.probe(servers[0])
    assert probe["ok"] and "add" in probe["tools"]
    assert mcp_client.load_servers(str(tmp_path / "absent.yaml")) == []


# ----------------------------------------------------------------------------- mission blocks and validation

GOOD_BLOCK = """Here is the plan.

```mission
statement: Ship the landing page
nodes:
  - title: Draft copy
    executor: model
    task_type: writing
    instruction: Write the hero copy
    output: both
  - title: Lock it
    executor: connector
    config: {connector: vault.save_artifact, name: hero.md}
    inputs: [1]
    on_failure: skip
```
"""


def test_parse_mission_block_accepts_good_blocks_and_rejects_bad_ones():
    parsed = missions.parse_mission_block(GOOD_BLOCK)
    assert parsed["statement"] == "Ship the landing page" and len(parsed["plan"]) == 2
    first, second = parsed["plan"]
    assert first["executor"] == "model" and first["type"] == "writing" and first["description"] == "Write the hero copy" and first["output"] == "both"
    assert second["executor"] == "connector" and second["config"]["connector"] == "vault.save_artifact" and second["inputs"] == [1] and second["on_failure"] == "skip"
    assert missions.parse_mission_block("no block here") is None
    with pytest.raises(missions.MissionBlockError, match="executor"):
        missions.parse_mission_block("```mission\nstatement: x\nnodes:\n  - title: a\n    executor: rocket\n```")
    with pytest.raises(missions.MissionBlockError, match="statement"):
        missions.parse_mission_block("```mission\nnodes:\n  - title: a\n```")
    with pytest.raises(missions.MissionBlockError, match="at most"):
        missions.parse_mission_block("```mission\nstatement: x\nnodes:\n" + "".join(f"  - title: n{i}\n" for i in range(13)) + "```")
    with pytest.raises(missions.MissionBlockError, match="YAML"):
        missions.parse_mission_block("```mission\nstatement: [unclosed\n```")
    block = missions.mission_block(parsed["statement"], parsed["plan"])
    again = missions.parse_mission_block(block)
    assert again["statement"] == parsed["statement"] and [n["title"] for n in again["plan"]] == ["Draft copy", "Lock it"]
    assert again["plan"][1]["config"] == second["config"] and again["plan"][1]["inputs"] == [1]


def test_validate_nodes_lists_every_reason():
    plan = missions.normalise_plan([
        {"title": "a", "executor": "connector", "config": {"connector": "nope"}},
        {"title": "b", "executor": "connector", "config": {"connector": "github.push"}},
        {"title": "c", "executor": "connector", "config": {"connector": "mcp.call", "server": "ghost", "tool": "x"}},
        {"title": "d", "executor": "sub_mission", "config": {}},
        {"title": "e", "executor": "model", "inputs": [7]},
    ])
    reasons = connectors_nodes.validate_nodes(plan, push_armed=False, mcp_servers=["fake"])
    joined = "\n".join(reasons)
    assert "unknown connector 'nope'" in joined and "github.push needs owner_repo" in joined and "push slot armed" in joined
    assert "no MCP server named 'ghost'" in joined and "sub-mission needs a statement" in joined and "input 7 must be an earlier step" in joined
    assert connectors_nodes.validate_nodes(missions.normalise_plan([{"title": "ok"}]), push_armed=False) == []
    assert connectors_nodes.validate_nodes([{"title": "x", "executor": "warp"}], push_armed=True) == ["step 1 (x): unknown executor 'warp'"]


# ----------------------------------------------------------------------------- node execution

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    monkeypatch.setattr(mission_runner, "cortex_wait_seconds", lambda ledger, messages, budget: 0.0)
    return tmp_path


def run_plan(scope, goal, plan, secrets=None, mode="normal"):
    thread = int(vault.active_thread(scope, "task_finder")["id"])
    job_id = jobs.enqueue(scope, mission_runner.KIND, {"goal": goal, "plan": plan, "mode": mode, "max_tokens": 500}, secrets or {}, thread_id=thread)
    jobs.run_job(vault.claim_job("t", (mission_runner.KIND,)))
    return thread, vault.job_view(vault.job_by_id(job_id))


def echo_model(calls: List[dict]):
    def fake_generate(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        calls.append({"prompt": messages[-1]["content"], "messages": messages, "max_tokens": max_tokens})
        return f"answer {len(calls)}: {messages[-1]['content'][:30]}", RouteDecision("fake", "m", task_type, "r")
    return fake_generate


def test_connector_nodes_lock_artifacts_push_and_revert_with_the_session_token(db, monkeypatch):
    from orchestrator import github_push as gp
    from tests.test_github_push import make_fake
    calls: List[dict] = []
    api_calls = []
    monkeypatch.setattr(mission_runner, "generate_mode", echo_model(calls))
    monkeypatch.setattr(gp.requests, "request", make_fake(api_calls))
    plan = [
        {"title": "Write the doc", "executor": "model", "type": "writing", "description": "Write the runbook", "output": "artifact"},
        {"title": "Lock it", "executor": "connector", "config": {"connector": "vault.save_artifact", "name": "runbook.md", "path": "docs/runbook.md"}},
        {"title": "Push", "executor": "connector", "config": {"connector": "github.push", "owner_repo": "me/proj", "files": [{"path": "docs/RUNBOOK.md", "body": "# run\n"}], "branch": "mission/runbook"}},
        {"title": "Undo", "executor": "connector", "config": {"connector": "github.revert"}},
        {"title": "Transcript", "executor": "connector", "config": {"connector": "vault.export_thread"}},
    ]
    thread, view = run_plan("s", "Document and push", plan, secrets={"github_token": "ghp_secret_token_123"})
    assert view["status"] == "done", view
    result = view["result"]
    assert result["succeeded"] == 5 and result["failed"] == 0 and result["stopped_at"] == ""
    assert result["node_artifacts"][0]["step"] == "1" and result["saved_artifacts"]
    assert result["push_records"][0]["branch"] == "mission/runbook" and result["push_records"][0]["pr_url"].endswith("/pull/101")
    rows = vault.recent_messages("s", 20, thread_id=thread)
    assistant = [r for r in rows if r["role"] == "assistant"]
    assert assistant[0]["content"].startswith("Locked as artifact v1") and "answer 1" in assistant[0]["content"]  # output: artifact keeps the chat short
    assert assistant[2]["provider"] == "local-executor/github.push" and "pull request #101" in assistant[2]["content"]
    assert "Revert pull request #102" in assistant[3]["content"]
    assert assistant[4]["content"].startswith("Transcript ")
    assert vault.artifact_by_id(result["node_artifacts"][0]["artifact_id"])["file_path"] == f"missions/node-{thread}-1.md"
    assert "ghp_secret_token_123" not in json.dumps(view)


def test_push_node_refuses_without_the_token_and_the_policy_stops_the_mission(db, monkeypatch):
    calls: List[dict] = []
    monkeypatch.setattr(mission_runner, "generate_mode", echo_model(calls))
    plan = [
        {"title": "Push", "executor": "connector", "config": {"connector": "github.push", "owner_repo": "me/proj", "files": [{"path": "a", "body": "b"}]}},
        {"title": "After", "executor": "model", "description": "never runs"},
    ]
    _, view = run_plan("s", "Push without a token", plan)
    result = view["result"]
    assert result["failed"] == 1 and result["succeeded"] == 0 and result["stopped_at"] == "Push"
    assert "GitHub token" in result["failures"][0][1] and result["failures"][1][0] == "steps 2–2" and "not run" in result["failures"][1][1]
    assert calls == []
    assert vault.thread_by_id(int(vault.active_thread("s", "task_finder")["id"]))["mission"] is None or not result["succeeded"]


def test_failure_policies_skip_and_retry_once(db, monkeypatch):
    attempts = {"n": 0}

    def flaky(mode, task_type, messages, ledger, max_tokens=4096, temperature=0.2, paid_slot=None):
        attempts["n"] += 1
        if task_type == "flaky" and attempts["n"] < 2:
            raise RuntimeError("blip")
        if task_type == "dead":
            raise RuntimeError("dead")
        return "fine", RouteDecision("fake", "m", task_type, "r")

    monkeypatch.setattr(mission_runner, "generate_mode", flaky)
    plan = [
        {"title": "Retry me", "executor": "model", "type": "flaky", "description": "x", "on_failure": "retry_once"},
        {"title": "Skip me", "executor": "model", "type": "dead", "description": "y", "on_failure": "skip"},
        {"title": "Still runs", "executor": "model", "type": "chat", "description": "z"},
    ]
    _, view = run_plan("s", "Policies", plan)
    result = view["result"]
    assert result["succeeded"] == 2 and result["failed"] == 1 and result["stopped_at"] == ""
    assert result["failures"] == [["Skip me", "dead"]] and attempts["n"] == 4


def test_inputs_are_prepended_and_sub_missions_run_nested_titles(db, monkeypatch):
    calls: List[dict] = []
    monkeypatch.setattr(mission_runner, "generate_mode", echo_model(calls))
    plan = [
        {"title": "Facts", "executor": "model", "description": "List three facts"},
        {"title": "Filler", "executor": "model", "description": "Unrelated"},
        {"title": "Use facts", "executor": "model", "description": "Summarise the facts", "inputs": [1]},
        {"title": "Essay", "executor": "sub_mission", "config": {"statement": "Write a 1 page essay on tides", "sections": 2}, "output": "artifact"},
    ]
    thread, view = run_plan("s", "Nested", plan)
    result = view["result"]
    assert view["status"] == "done" and result["failed"] == 0 and result["succeeded"] == 4, result
    third = calls[2]["messages"]
    assert any("Output of step 1 · Facts" in m["content"] and "answer 1" in m["content"] for m in third)
    assert not any("answer 2" in m["content"] and "Output of step 2" in m["content"] for m in third)
    rows = vault.recent_messages("s", 60, thread_id=thread)
    user_titles = [r["content"].split("]")[0] + "]" for r in rows if r["role"] == "user" and r["content"].startswith("[")]
    assert "[[Sub 4.1] Brief]" in user_titles or any(t.startswith("[[Sub 4.") for t in user_titles), user_titles
    sub_rows = [r for r in rows if r["role"] == "assistant" and r["provider"] == "local-executor/sub_mission"]
    assert len(sub_rows) == 1 and sub_rows[0]["content"].startswith("Locked as artifact")
    assert result["node_artifacts"][-1]["step"] == "4"
    body = vault.export_artifact(result["node_artifacts"][-1]["artifact_id"])[1]
    assert "tides" in body.lower() and "answer" in body


def test_mission_nodes_round_trip_and_old_payloads_still_run(db, monkeypatch):
    thread = int(vault.active_thread("s", "task_finder")["id"])
    plan = missions.normalise_plan([{"title": "a", "executor": "connector", "config": {"connector": "vault.export_thread"}, "output": "both"}])
    assert vault.save_mission_nodes(thread, plan) == 1
    stored = vault.mission_nodes_for(thread)
    assert stored[0]["config"] == {"connector": "vault.export_thread"} and stored[0]["output"] == "both" and stored[0]["executor"] == "connector"
    assert vault.mission_nodes_for(thread + 99) == []
    calls: List[dict] = []
    monkeypatch.setattr(mission_runner, "generate_mode", echo_model(calls))
    old_plan = [{"id": 1, "kind": "research", "title": "Old", "type": "chat", "description": "no node fields at all", "status": "queued"}]
    _, view = run_plan("s", "Legacy", old_plan)
    assert view["status"] == "done" and view["result"]["succeeded"] == 1 and calls[0]["max_tokens"] == 500


def test_mcp_call_node_runs_a_declared_server(db, tmp_path, monkeypatch):
    path = tmp_path / "servers.yaml"
    path.write_text(json.dumps({"servers": [fake_server()]}))
    monkeypatch.setenv("CHAT_JOHNSON_MCP_SERVERS", str(path))
    plan = [
        {"title": "Add", "executor": "connector", "config": {"connector": "mcp.call", "server": "fake", "tool": "add", "arguments": {"a": 1, "b": 2}}},
        {"title": "Bad", "executor": "connector", "config": {"connector": "mcp.call", "server": "fake", "tool": "nope"}, "on_failure": "skip"},
    ]
    thread, view = run_plan("s", "MCP", plan)
    result = view["result"]
    assert result["succeeded"] == 1 and result["failed"] == 1 and result["mcp_results"][0]["content"][0]["text"] == "3.0"
    assert "unknown tool" in result["failures"][0][1] and "ghp_abcdefghijklmnopqrstuvwxyz" not in json.dumps(result)


# ----------------------------------------------------------------------------- Heavy streaming

class FakeStream:
    def __init__(self, task_type, messages, ledger=None, max_tokens=4096, temperature=0.2, system_prompt=""):
        if task_type == "explode":
            raise ProviderError("no endpoint")
        self.decision = RouteDecision("free", "stream-model", task_type, "milp")
        self.text = ""
        self.messages = messages

    def __iter__(self):
        for chunk in ("final ", "answer"):
            self.text += chunk
            yield chunk
        self.decision.finish = "stop"


def test_heavy_stream_returns_an_unexhausted_synthesis_stream(monkeypatch):
    log = []

    def fake_generate(task_type, messages, ledger=None, max_tokens=4096, temperature=0.2, system_prompt=""):
        log.append(task_type)
        return f"{task_type}-text", RouteDecision("free", "m", task_type, "r")

    monkeypatch.setattr(router, "cortex_available", lambda: True)
    monkeypatch.setattr(router, "cortex_generate", fake_generate)
    monkeypatch.setattr(router, "CortexStream", FakeStream)
    stream, decision = router.heavy_stream("chat", [{"role": "user", "content": "q"}], None, max_tokens=900)
    assert isinstance(stream, FakeStream) and log == ["chat", "reasoning"] and stream.text == ""
    assert "".join(stream) == "final answer" and stream.text == "final answer" and stream.decision.finish == "stop"
    assert decision is stream.decision and "draft -> review(free) -> synthesis" in decision.reason
    payload = json.loads(stream.messages[-1]["content"])
    assert payload["candidate"] == "chat-text" and payload["review"] == "reasoning-text"
    text, decision = router.heavy_stream("explode", [{"role": "user", "content": "q"}], None, max_tokens=900)
    assert text == "explode-text" and "synthesis was unavailable" in decision.reason
    monkeypatch.setattr(router, "cortex_available", lambda: False)
    with pytest.raises(ProviderError):
        router.heavy_stream("chat", [{"role": "user", "content": "q"}], None)
