"""Batch G1: shared-host lockdown, VM stack, encrypted job secrets, quota caps, job hygiene, scope, sandbox, GitHub, MCP."""
import io
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
from typing import List

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (  # noqa: E402
    connectors_nodes, envsafe, errors, github_push as gp, github_repo as gr, jobs, jobsecrets, mcp_client, missions,
    patches, quota_registry, repo_ingest, router, sandbox, test_loop, vault, webqa,
)
from orchestrator.config import bind_session_keys  # noqa: E402
from orchestrator.quota import QuotaLedger  # noqa: E402
from orchestrator.router import ProviderError, RouteDecision  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_ENVS = ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    monkeypatch.delenv("CHAT_JOHNSON_JOB_KEY", raising=False)
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    vault.initialize_database()
    quota_registry.reset_for_tests()
    bind_session_keys({})
    yield tmp_path
    bind_session_keys({})
    quota_registry.reset_for_tests()


# ----------------------------------------------------------------------------- shared-host lockdown

def test_minimal_env_drops_keys_and_keeps_the_allowlist(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    monkeypatch.setenv("CHAT_JOHNSON_JOB_KEY", "k")
    env = envsafe.minimal_env({"FAKE_MCP_SECRET": "x"})
    assert "GROQ_API_KEY" not in env and "CHAT_JOHNSON_JOB_KEY" not in env
    assert env["PATH"] and env["FAKE_MCP_SECRET"] == "x" and env["PYTHONDONTWRITEBYTECODE"] == "1"
    monkeypatch.setenv("CHAT_JOHNSON_SELF_HOSTED", "1")
    assert envsafe.self_hosted()
    monkeypatch.delenv("CHAT_JOHNSON_SELF_HOSTED")
    assert not envsafe.self_hosted()


def test_ingestion_skips_secret_files_and_extensionless_unknowns(tmp_path):
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_secret\n")
    (tmp_path / ".streamlit").mkdir()
    (tmp_path / ".streamlit" / "secrets.toml").write_text('key = "AIzaSy-secret"\n')
    (tmp_path / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (tmp_path / "server.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    (tmp_path / "mystery").write_text("plain text with no extension\n")
    (tmp_path / "Dockerfile").write_text("FROM python\n")
    (tmp_path / "app.py").write_text("print(1)\n")
    (tmp_path / ".env.example").write_text("GROQ_API_KEY=\n")
    files = repo_ingest.walk_repo(str(tmp_path))
    assert set(files) == {"Dockerfile", "app.py", ".env.example"}, files
    text, _ = repo_ingest.serialize_repo(str(tmp_path))
    assert "gsk_secret" not in text and "AIzaSy-secret" not in text


def test_pytest_in_the_sandbox_never_sees_the_operators_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret_value")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_env.py").write_text("import os\n\ndef test_env():\n    assert 'GROQ_API_KEY' not in os.environ\n")
    passed, output = test_loop.run_pytest(str(tmp_path), timeout=120)
    assert passed, output


def test_web_qa_refuses_private_and_non_http_targets():
    assert "private" in webqa.unsafe_target("http://127.0.0.1:8501/")
    assert "private" in webqa.unsafe_target("http://169.254.169.254/latest/meta-data/")
    assert "http(s)" in webqa.unsafe_target("file:///etc/passwd")
    assert "local" in webqa.unsafe_target("http://localhost/") and "local" in webqa.unsafe_target("http://ollama:11434/")
    assert webqa.unsafe_target("http://127.0.0.1:1/", allow_private=True) == ""
    result = webqa.check_url("http://127.0.0.1:1/")
    assert not result["ok"] and result["error"].startswith("refused:")
    browser = webqa.browser_check("file:///etc/passwd", [{"expect_text": "root"}])
    assert not browser["ok"] and browser["error"].startswith("refused:")
    shot = webqa._screenshot_path("../../etc/x")
    assert shot.endswith("x.png") and ".." not in shot and shot.startswith(os.path.join(__import__("tempfile").gettempdir(), "chat-johnson-webqa"))


def test_repository_run_and_local_paths_are_gated_to_the_self_hosted_vm(monkeypatch):
    monkeypatch.delenv("CHAT_JOHNSON_SELF_HOSTED", raising=False)
    plan = missions.normalise_plan([
        {"title": "run", "executor": "connector", "config": {"connector": "repository.run", "goal": "x", "path": "/srv/repo", "test_rounds": 2}},
        {"title": "push", "executor": "connector", "config": {"connector": "github.push", "owner_repo": "me/proj"}},
    ])
    reasons = "\n".join(connectors_nodes.validate_nodes(plan, push_armed=True, secrets_deliverable=False))
    assert "local path runs only on the self-hosted VM" in reasons and "test_rounds must be 0" in reasons and "no worker here can receive one" in reasons
    assert connectors_nodes.validate_nodes(plan, push_armed=True, hosted=True, secrets_deliverable=True) == []

    class Ctx:
        project_scope = "s"
        thread_id = 1
        secrets = {}
        ledger = None

    with pytest.raises(ValueError, match="self-hosted"):
        connectors_nodes.repository_run(Ctx(), {"goal": "x", "path": "/srv/repo"}, [], {})
    with pytest.raises(ValueError, match="test_rounds"):
        connectors_nodes.repository_run(Ctx(), {"goal": "x", "test_rounds": 1}, [], {"repo_path": "/srv/repo"})


# ----------------------------------------------------------------------------- VM stack

def test_root_vm_stack_keeps_keys_in_the_worker_and_never_serves_plain_http_to_the_internet():
    compose = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.yml")))
    services = compose["services"]
    assert "env_file" not in services["app"] and services["worker"]["env_file"] == ".env"
    assert [name for name, svc in services.items() if svc.get("ports")] == ["caddy"]
    assert "127.0.0.1:8080" in services["caddy"]["ports"][0]
    caddy = open(os.path.join(ROOT, "Caddyfile")).read()
    assert "{$DOMAIN::80}" in caddy and "basic_auth" in caddy and not caddy.lstrip("#").strip().startswith(":80")
    assert "basic_auth" not in open(os.path.join(ROOT, "Caddyfile.open")).read()
    env_example = open(os.path.join(ROOT, ".env.example")).read()
    assert "CHAT_JOHNSON_JOB_KEY=" in env_example and "CADDY_HASH=" in env_example and "DOMAIN=" in env_example
    bootstrap = open(os.path.join(ROOT, "scripts", "vm-bootstrap.sh")).read()
    assert "hash-password" in bootstrap and "CHAT_JOHNSON_JOB_KEY" in bootstrap and "chmod 600 .env" in bootstrap
    backup = open(os.path.join(ROOT, "scripts", "vm-backup.sh")).read()
    assert "umask 077" in backup and "rm -f /data/vault-backup.db" in backup
    from orchestrator import deploykit as dk
    kit = dk.generate_kit(dk.KitSpec(target="oracle-vm"))
    findings = dk.validate_kit(kit)
    assert all(f.level == "ok" for f in findings), [f for f in findings if f.level != "ok"]
    bad = [dk.KitFile("docker-compose.yml", "services:\n  app:\n    env_file: .env\n    ports: ['80:80']\n", "yaml", ""), dk.KitFile("Caddyfile", ":80 {\n  reverse_proxy app:8501\n}\n", "text", "")]
    levels = {f.path: f.level for f in dk.validate_kit(bad)}
    assert levels == {"docker-compose.yml": "error", "Caddyfile": "error"}
    assert dk.KitSpec(target="oracle-vm", language="node", port="abc").normalized().language == "python"
    assert dk.KitSpec(port="abc").normalized().port == 8501


# ----------------------------------------------------------------------------- encrypted job secrets

def test_jobsecrets_round_trip_and_graceful_absence(monkeypatch):
    monkeypatch.delenv("CHAT_JOHNSON_JOB_KEY", raising=False)
    assert not jobsecrets.available() and jobsecrets.encrypt({"a": "b"}) is None and jobsecrets.decrypt(b"x") == {}
    monkeypatch.setenv("CHAT_JOHNSON_JOB_KEY", jobsecrets.generate_key())
    assert jobsecrets.available()
    blob = jobsecrets.encrypt({"github_token": "ghp_secret_1234567890", "empty": ""})
    assert blob and b"ghp_" not in blob
    assert jobsecrets.decrypt(blob) == {"github_token": "ghp_secret_1234567890"}
    monkeypatch.setenv("CHAT_JOHNSON_JOB_KEY", jobsecrets.generate_key())
    assert jobsecrets.decrypt(blob) == {}  # another key reads nothing, never raises
    monkeypatch.setenv("CHAT_JOHNSON_JOB_KEY", "not-a-key")
    assert not jobsecrets.available()


def test_enqueue_hands_secrets_to_the_worker_encrypted_and_deletes_them_on_claim(db, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_JOB_KEY", jobsecrets.generate_key())
    monkeypatch.setattr(jobs, "_RUNNER", None)
    monkeypatch.setenv("CHAT_JOHNSON_JOB_WORKERS", "0")
    assert jobs.secrets_deliverable()  # the key makes the hand-off possible without local workers
    seen = {}
    jobs.register_handler("g_secret", lambda ctx: seen.update(ctx.secrets) or {"ok": True})
    job_id = jobs.enqueue("s", "g_secret", {"goal": "x"}, {"github_token": "ghp_secret_1234567890"})
    with jobs._SECRETS_LOCK:
        assert job_id not in jobs._SECRETS  # nothing in memory
    assert vault.has_job_secrets(job_id) and jobs.secrets_held(job_id)
    with vault._open_database() as connection:
        blob = connection.execute("SELECT blob FROM job_secrets WHERE job_id = ?", (job_id,)).fetchone()["blob"]
    assert b"ghp_secret" not in bytes(blob)
    jobs.run_job(vault.claim_job("w:1:abc", ("g_secret",)))
    assert seen == {"github_token": "ghp_secret_1234567890"}
    assert not vault.has_job_secrets(job_id) and vault.job_by_id(job_id)["status"] == "done"
    # Without the key and without workers, the UI is told a token cannot be delivered.
    monkeypatch.delenv("CHAT_JOHNSON_JOB_KEY")
    assert not jobs.secrets_deliverable()
    later = jobs.enqueue("s", "g_secret", {}, {"github_token": "ghp_x"})
    assert jobs.secrets_held(later) and not vault.has_job_secrets(later)  # memory, bounded by the TTL
    with jobs._SECRETS_LOCK:
        jobs._SECRETS[later] = (time.time() - jobs.SECRET_TTL_SECONDS - 1, {"github_token": "ghp_x"})
    jobs.enqueue("s", "g_secret", {}, {"github_token": "ghp_y"})  # the next enqueue purges the stale entry
    assert not jobs.secrets_held(later)


# ----------------------------------------------------------------------------- quota

def test_daily_cap_blocks_the_cortex_path_with_a_plain_sentence(db):
    bind_session_keys({"GROQ_API_KEY": "gsk_test_key_value"})
    ledger = quota_registry.get_quota_ledger()
    ledger.set_daily_limit("groq", 1000)
    ledger.record("groq", 990)
    with pytest.raises(ProviderError) as excinfo:
        router.select_milp_endpoint("chat", 50, ledger)
    assert "daily cap" in str(excinfo.value)
    sentence = errors.plain_error(excinfo.value)
    assert sentence.startswith("The daily token cap is reached") and "resets" in sentence
    assert ledger.wait_seconds("groq", 50) > 60
    ledger2 = quota_registry.get_quota_ledger()
    ledger2.set_daily_limit("groq", 10_000)
    assert router.select_milp_endpoint("chat", 50, ledger2).endpoint.name == "groq"  # under the cap it is selectable again


def test_buckets_are_per_credential_persisted_and_capped_for_every_vendor(db):
    bind_session_keys({"GROQ_API_KEY": "gsk_first"})
    one = quota_registry.get_quota_ledger()
    one.record("groq", 500)
    bind_session_keys({"GROQ_API_KEY": "gsk_first", "GEMINI_API_KEY": "AIza_second"})
    two = quota_registry.get_quota_ledger()
    assert two.usage("groq")["daily_tokens"] == 500  # adding a key never resets another vendor's counter
    bind_session_keys({"GROQ_API_KEY": "gsk_other"})
    assert quota_registry.get_quota_ledger().usage("groq")["daily_tokens"] == 0  # a different credential is a different bucket
    from orchestrator.config import daily_cap
    assert two.daily_limit("huggingface") == daily_cap("huggingface") > 0 and two.daily_limit("local") == daily_cap("local")
    # Persisted: a fresh process (registry reset) reads today's count back from the vault.
    quota_registry.reset_for_tests()
    bind_session_keys({"GROQ_API_KEY": "gsk_first"})
    assert quota_registry.get_quota_ledger().usage("groq")["daily_tokens"] == 500
    assert vault.quota_usage_load(quota_registry.bucket_key("groq"), time.strftime("%Y-%m-%d", time.gmtime())) == (500, 1)


def test_streams_are_charged_on_raw_text_and_on_failure(db, monkeypatch):
    bind_session_keys({"GROQ_API_KEY": "gsk_test_key_value"})
    ledger = quota_registry.get_quota_ledger()
    hidden = "<think>" + "x" * 4000 + "</think>final answer"

    def fake_stream(endpoint, messages, max_tokens=4096, temperature=0.2, system_prompt="", ledger=None, status=None, timeout=None):
        for piece in (hidden[:2000], hidden[2000:]):
            yield piece
        if status is not None:
            status["finish"] = "stop"

    monkeypatch.setattr(router, "cortex_stream", fake_stream)
    text, decision = router.cortex_generate("chat", [{"role": "user", "content": "q"}], ledger, max_tokens=100)
    assert text == "final answer" and ledger.usage("groq")["tpm_used"] > 900

    def failing_stream(endpoint, messages, max_tokens=4096, temperature=0.2, system_prompt="", ledger=None, status=None, timeout=None):
        yield "partial " * 100
        raise ProviderError("groq stream failed after some text: ConnectionError")

    quota_registry.reset_for_tests()
    ledger = quota_registry.get_quota_ledger()
    monkeypatch.setattr(router, "cortex_stream", failing_stream)
    with pytest.raises(ProviderError):
        router.cortex_generate("chat", [{"role": "user", "content": "q"}], ledger, max_tokens=100)
    assert ledger.usage("groq")["tpm_used"] >= 150  # the partial stream was charged
    stream = router.CortexStream("chat", [{"role": "user", "content": "q"}], ledger, max_tokens=100)
    with pytest.raises(ProviderError):
        list(stream)
    assert stream.text.startswith("partial") and ledger.usage("groq")["tpm_used"] >= 300


def test_legacy_generate_registers_missing_buckets_instead_of_raising_keyerror(monkeypatch):
    bind_session_keys({"NVIDIA_API_KEY": "nvapi-test"})
    monkeypatch.setattr(router, "chat", lambda *a, **k: (_ for _ in ()).throw(ProviderError("nvidia HTTP 503: down")))
    with pytest.raises(ProviderError):
        router.generate("chat", [{"role": "user", "content": "q"}], QuotaLedger({}))
    bind_session_keys({})


class DraftThenFail:
    def __init__(self, task_type, messages, ledger=None, max_tokens=4096, temperature=0.2, system_prompt=""):
        self.decision = RouteDecision("free", "stream-model", task_type, "milp")
        self.text = ""
        self.messages = messages

    def __iter__(self):
        raise ProviderError("connection reset before any text")
        yield  # pragma: no cover


def test_heavy_stream_falls_back_to_the_draft_when_the_synthesis_stream_dies(monkeypatch):
    def fake_generate(task_type, messages, ledger=None, max_tokens=4096, temperature=0.2, system_prompt=""):
        return f"{task_type}-text", RouteDecision("free", "m", task_type, "r")

    monkeypatch.setattr(router, "cortex_available", lambda: True)
    monkeypatch.setattr(router, "cortex_generate", fake_generate)
    monkeypatch.setattr(router, "CortexStream", DraftThenFail)
    stream, decision = router.heavy_stream("chat", [{"role": "user", "content": "q"}], None, max_tokens=900)
    assert "".join(stream) == "chat-text" and stream.text == "chat-text" and stream.fell_back
    assert "draft returned" in stream.decision.reason and stream.decision.model == "m"


def test_strip_reasoning_tags_matches_the_live_view_for_an_unterminated_block():
    assert router.strip_reasoning_tags("Answer starts <think>never closed") == "Answer starts"
    assert list(router._visible_chunks(["Answer starts <think>never closed"])) == ["Answer starts "]


# ----------------------------------------------------------------------------- jobs

def test_worker_names_are_unique_and_a_restarted_host_fails_its_predecessors_rows(db):
    a, b = jobs.JobRunner(max_workers=0), jobs.JobRunner(max_workers=0)
    assert a.worker_name != b.worker_name and a.worker_name.startswith(f"{a.host}:")
    jobs.register_handler("g_hb", lambda ctx: {})
    mine = jobs.enqueue("s", "g_hb", {}, {})
    theirs = jobs.enqueue("s", "g_hb", {}, {})
    other = jobs.enqueue("s", "g_hb", {}, {})
    vault.claim_job(f"{a.host}:1:aaaaaa", ("g_hb",))
    vault.claim_job(f"{a.host}:2:bbbbbb", ("g_hb",))
    vault.claim_job("elsewhere:9:cccccc", ("g_hb",))
    assert vault.fail_jobs_of_host(f"{a.host}:", "restarted", except_worker=f"{a.host}:2:bbbbbb") == 1
    assert vault.job_by_id(mine)["status"] == "failed" and vault.job_by_id(theirs)["status"] == "running" and vault.job_by_id(other)["status"] == "running"
    assert vault.touch_heartbeat([]) == 0


def test_a_failing_finish_does_not_kill_the_worker_pool(db, monkeypatch):
    jobs.register_handler("g_boom", lambda ctx: {"ok": True})
    first = jobs.enqueue("s", "g_boom", {}, {})
    real_finish = vault.finish_job
    calls = {"n": 0}

    def flaky_finish(job_id, status, result):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_finish(job_id, status, result)

    monkeypatch.setattr(jobs.vault, "finish_job", flaky_finish)
    runner = jobs.JobRunner(max_workers=1, poll_seconds=0.05).start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and calls["n"] < 2:
            time.sleep(0.05)
        second = jobs.enqueue("s", "g_boom", {}, {})
        deadline = time.time() + 5
        while time.time() < deadline and vault.job_by_id(second)["status"] != "done":
            time.sleep(0.05)
        assert vault.job_by_id(second)["status"] == "done" and runner.alive == 2  # the pool survived the locked database
        assert vault.job_by_id(first)["status"] in ("failed", "running") and calls["n"] >= 3  # retried, then recorded as failed
    finally:
        runner.stop()


def test_every_writer_redacts_secrets(db):
    key = "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"
    thread = vault.create_thread("s", title=f"chat about {key}")
    assert key not in vault.thread_by_id(thread)["title"]
    vault.rename_thread(thread, f"renamed {key}")
    vault.set_thread_mission(thread, f"mission with {key}")
    row = vault.thread_by_id(thread)
    assert key not in row["title"] and key not in row["mission"]
    jobs.register_handler("g_redact", lambda ctx: {})
    job_id = jobs.enqueue("s", "g_redact", {}, {})
    vault.claim_job("w", ("g_redact",))
    vault.update_job_progress(job_id, {"text": f"Step failed once (request to https://x/?key={key})"})
    vault.ask_job_question(job_id, f"Use {key}?")
    stored = vault.job_by_id(job_id)
    assert key not in stored["progress"] and key not in stored["question"]
    assert key not in errors.plain_error(RuntimeError(f"unexpected response: {key}"))


# ----------------------------------------------------------------------------- scope isolation

@pytest.mark.parametrize("reader", ["thread_by_id", "clear_thread", "delete_thread", "rename_thread", "set_thread_mission", "messages_around", "thread_outline", "mission_nodes_for", "thread_transcript", "export_thread", "switch_thread"])
def test_thread_readers_refuse_another_scope(db, reader):
    victim = int(vault.active_thread("victim", "normal_chat")["id"])
    vault.append_message("victim", "user", "private text", thread_id=victim)
    args = {"rename_thread": (victim, "x"), "set_thread_mission": (victim, "x"), "messages_around": (victim, 1)}.get(reader, (victim,))
    kwargs = {"project_scope": "attacker"}
    with pytest.raises(vault.ScopeMismatch):
        getattr(vault, reader)(*args, **kwargs)
    getattr(vault, reader)(*args, project_scope="victim")  # the owner is fine


def test_artifact_job_and_connector_reads_refuse_another_scope(db):
    artifact_id, _ = vault.save_artifact("victim", "notes.md", "docs/secret-notes.md", "victim private text", "markdown")
    with pytest.raises(vault.ScopeMismatch):
        vault.export_artifact(artifact_id, "attacker")
    with pytest.raises(vault.ScopeMismatch):
        vault.artifact_by_id(artifact_id, "attacker")
    assert vault.export_artifact(artifact_id, "victim")[1] == "victim private text"
    with pytest.raises(vault.ScopeMismatch):
        connectors_nodes._files_for_push({"from_artifacts": [artifact_id]}, [], {}, "attacker")
    jobs.register_handler("g_scope", lambda ctx: {})
    job_id = jobs.enqueue("victim", "g_scope", {}, {})
    for fn in (vault.job_by_id, vault.request_cancel):
        with pytest.raises(vault.ScopeMismatch):
            fn(job_id, "attacker")
    with pytest.raises(vault.ScopeMismatch):
        vault.answer_job(job_id, "x", "attacker")
    thread = int(vault.active_thread("victim", "task_finder")["id"])
    vault.save_mission_nodes(thread, [{"title": "a"}], "victim")
    with vault._open_database() as connection:
        assert connection.execute("SELECT project_scope FROM mission_nodes WHERE thread_id = ?", (thread,)).fetchone()["project_scope"] == "victim"
    vault.delete_thread(thread, "victim")
    assert vault.mission_nodes_for(thread) == []


# ----------------------------------------------------------------------------- mission block hardening

def test_mission_block_ids_are_positions_inputs_must_be_lists_and_deep_yaml_is_refused():
    parsed = missions.parse_mission_block("```mission\nstatement: s\nnodes:\n  - title: a\n    id: 7\n  - title: b\n    id: 7\n    inputs: [1]\n```")
    assert [n["id"] for n in parsed["plan"]] == [1, 2]
    with pytest.raises(missions.MissionBlockError, match="list"):
        missions.parse_mission_block("```mission\nstatement: s\nnodes:\n  - title: a\n  - title: b\n    inputs: '12'\n```")
    with pytest.raises(missions.MissionBlockError):
        missions.parse_mission_block("```mission\nstatement: " + "[" * 3000 + "\n```")
    with pytest.raises(missions.MissionBlockError, match="larger"):
        missions.parse_mission_block("```mission\nstatement: s\nnodes:\n  - title: " + "x" * 25000 + "\n```")


# ----------------------------------------------------------------------------- sandbox and GitHub

def test_model_output_cannot_write_into_git_or_orchestrator_directories():
    for path in (".git/config", ".git", ".orchestrator/memory.json", "src/.git/hooks/pre-commit", ".GIT/config", ".hg/hgrc"):
        assert not patches._safe_relpath(path), path
    assert patches._safe_relpath("src/app.py") and patches._safe_relpath(".github/workflows/ci.yml")
    assert patches.parse_file_blocks("```file: .git/config\n[core]\n\tfsmonitor = touch /tmp/pwned\n```") == {}
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n+++ b/.git/config\n"
    assert patches.diff_paths(diff) == ["src/a.py"]


def _git(args: List[str], cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_worktree_sandboxes_are_detected_committed_and_cleaned_up(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], str(repo))
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root"], str(repo))
    (repo / "a.py").write_text("x = 1\n")
    _git(["add", "a.py"], str(repo))
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "a"], str(repo))
    staging = tmp_path / "staging"
    box, branch = sandbox.create_worktree(str(repo), str(staging))
    assert branch != "(copy-mode)" and os.path.isfile(os.path.join(box, ".git")) and patches.is_git_sandbox(box)
    assert sandbox.sandbox_source(box)["branch"] == branch
    (repo / "hooks").mkdir(exist_ok=True)
    open(os.path.join(box, "b.py"), "w").write("y = 2\n")
    assert "b.py" in patches.changed_files(box)
    assert sandbox.commit_sandbox(box, "orchestrator: test") is True
    assert "b.py" in sandbox.diff_vs_base(box, str(repo))
    assert branch in _git(["branch", "--list", branch], str(repo))
    sandbox.cleanup_worktree(str(repo), box)
    assert not os.path.exists(box) and _git(["branch", "--list", branch], str(repo)) == "" and sandbox.sandbox_source(box) is None
    assert "fsmonitor" not in _git(["config", "--list"], str(repo))


def test_initialize_repository_refuses_a_repository_that_gained_commits(monkeypatch):
    from tests.test_github_push import FakeResponse

    def fake_request(method, url, headers=None, json=None, timeout=None):
        if method == "GET" and "/git/ref/heads/main" in url:
            return FakeResponse(200, {"object": {"sha": "someone-pushed"}})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(gp.requests, "request", fake_request)
    writer = gp.GitHubWriter("ghp_secret_token_123", "me/blank")
    with pytest.raises(gp.GitHubPushError, match="no longer empty"):
        writer.initialize_repository([("README.md", "# hi\n")], "main", "Scaffold")


def test_tarball_extraction_is_bounded_and_keeps_modes(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, mode, body in (("repo-abc/run.sh", 0o755, b"#!/bin/sh\n"), ("repo-abc/a.txt", 0o644, b"hello")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            archive.addfile(info, io.BytesIO(body))
    data = buffer.getvalue()
    dest = tmp_path / "out"
    dest.mkdir()
    files, size = gr.extract_tarball(data, str(dest))
    assert files == 2 and os.stat(dest / "run.sh").st_mode & 0o111
    with pytest.raises(gr.GitHubRepoError, match="budget"):
        gr.extract_tarball(data, str(tmp_path / "small"), max_files=1)
    with pytest.raises(gr.GitHubRepoError, match="budget"):
        gr.extract_tarball(data, str(tmp_path / "tiny"), max_bytes=4)
    assert gr.changed_paths_from_diff("+++ b/my file.py\t2024-01-01\n") == ["my file.py"]


# ----------------------------------------------------------------------------- MCP client

FAKE_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def test_mcp_servers_start_from_a_minimal_environment(monkeypatch):
    monkeypatch.setenv("FAKE_MCP_SECRET", "inherited-from-the-worker")
    server = {"name": "fake", "command": sys.executable, "args": [FAKE_SERVER], "env": {}}
    with mcp_client.MCPSession(server, timeout=10) as session:
        assert mcp_client.result_text(session.call_tool("secret")) == "0"  # nothing leaked from os.environ
    server["env"] = {"FAKE_MCP_SECRET": "declared"}
    with mcp_client.MCPSession(server, timeout=10) as session:
        assert mcp_client.result_text(session.call_tool("secret")) == str(len("declared"))


def test_mcp_failed_handshake_closes_the_process_and_reports_stderr():
    with pytest.raises(mcp_client.MCPError) as excinfo:
        mcp_client.MCPSession({"name": "sleeper", "command": "sleep", "args": ["12345"]}, timeout=1, startup_timeout=1).__enter__()
    assert "did not answer initialize" in str(excinfo.value)
    assert not subprocess.run(["pgrep", "-f", "sleep 12345"], capture_output=True, text=True).stdout.strip()
    with pytest.raises(mcp_client.MCPError) as excinfo:
        mcp_client.MCPSession({"name": "crash", "command": sys.executable, "args": ["-c", "import sys; sys.stderr.write('boom token ghp_abcdefghijklmnopqrstuvwxyz0123456789\\n'); sys.exit(3)"]}, timeout=5, startup_timeout=5).__enter__()
    message = str(excinfo.value)
    assert "exited during initialize" in message and "server stderr: boom" in message and "ghp_abcdefghijklmnopqrstuvwxyz" not in message


# ----------------------------------------------------------------------------- the app on a shared host

@pytest.fixture()
def app(tmp_path, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.setenv("CHAT_JOHNSON_JOB_WORKERS", "0")
    monkeypatch.delenv("CHAT_JOHNSON_SELF_HOSTED", raising=False)
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delattr(st, "segmented_control", raising=False)
    return AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)


def test_shared_host_has_no_local_path_and_never_runs_repository_tests(app, monkeypatch):
    app.query_params["ws"] = "repository"
    app.run()
    assert not app.exception
    assert app.radio(key="repo_source_kind").options == ["GitHub repository"]
    assert not any(c.key == "repo_run_tests" for c in app.checkbox)
    assert any("not executed on a shared deployment" in c.value for c in app.caption)
    monkeypatch.setenv("CHAT_JOHNSON_SELF_HOSTED", "1")
    app.run()
    assert app.radio(key="repo_source_kind").options == ["GitHub repository", "Local path"]
    assert any(c.key == "repo_run_tests" for c in app.checkbox)


def test_session_model_override_disables_model_switching(monkeypatch):
    from tests.test_cortex import FakeResponse

    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CORTEX_GEMINI_MODEL", raising=False)
    bind_session_keys({"GEMINI_API_KEY": "AIza_session", "CORTEX_GEMINI_MODEL": "pinned-flash"})
    calls: List[str] = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append(url.split("/models/")[1].split(":")[0])
        return FakeResponse(status_code=404, text="model not found")

    monkeypatch.setattr(router.requests, "post", fake_post)
    monkeypatch.setattr(router.discovery, "sleep", lambda *a, **k: None)
    try:
        with pytest.raises(ProviderError):
            list(router.cortex_stream("google_ai_studio", [{"role": "user", "content": "x"}]))
        assert set(calls) == {"pinned-flash"}  # a session pin is honoured exactly like an environment pin
    finally:
        bind_session_keys({})
