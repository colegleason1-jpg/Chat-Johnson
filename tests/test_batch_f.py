"""Batch F: the local model endpoint, worker mode and heartbeats, tick bootstrap, and the VM kit."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import deploykit as dk  # noqa: E402
from orchestrator import jobs, router, vault  # noqa: E402
from orchestrator.providers import ProviderError  # noqa: E402
from orchestrator.quota import QuotaLedger  # noqa: E402
from orchestrator.society import tick  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    vault.initialize_database()
    yield "scope-f"


@pytest.fixture()
def no_local(monkeypatch):
    monkeypatch.delenv("CHAT_JOHNSON_LOCAL_ENDPOINT", raising=False)
    monkeypatch.delenv("CHAT_JOHNSON_LOCAL_KEY", raising=False)
    router.unregister_local_endpoint()
    yield
    router.unregister_local_endpoint()


def test_local_endpoint_registers_from_env_and_counts_as_keyed(no_local, monkeypatch):
    assert router.register_local_endpoint_from_env() is None and router.local_endpoint() is None
    monkeypatch.setenv("CHAT_JOHNSON_LOCAL_ENDPOINT", "http://ollama:11434/v1/")
    monkeypatch.setenv("CHAT_JOHNSON_LOCAL_MODEL", "qwen2.5:7b")
    endpoint = router.register_local_endpoint_from_env()
    assert endpoint.name == "local" and endpoint.base_url == "http://ollama:11434/v1" and router.endpoint_model(endpoint) == "qwen2.5:7b"
    assert router._endpoint_key(endpoint) == "local" and router.cortex_available()
    assert router._vendor(endpoint) == "local"
    from orchestrator.config import daily_cap
    assert daily_cap("local") == 5_000_000
    router.unregister_local_endpoint()
    assert "local" not in router.CORTEX_ENDPOINTS


def test_local_first_generate_prefers_the_local_model_and_falls_back(no_local, monkeypatch):
    ledger = QuotaLedger({"local": (60, 10**9)})
    messages = [{"role": "user", "content": "summarise this"}]
    called = []
    monkeypatch.setattr(router, "generate_mode", lambda *a, **k: (called.append("cloud") or ("cloud answer", router.RouteDecision("groq", "m", "quick_text", "r"))))
    text, decision = router.local_first_generate("normal", "quick_text", messages, ledger)
    assert text == "cloud answer" and called == ["cloud"]  # no local endpoint: straight to the router
    router.register_local_endpoint("http://ollama:11434/v1", "llama3.1:8b")

    def fake_stream(endpoint, msgs, max_tokens=4096, temperature=0.2, ledger=None, status=None, **kw):
        assert endpoint.name == "local"
        status["finish"] = "stop"
        yield "local "
        yield "answer"

    monkeypatch.setattr(router, "cortex_stream", fake_stream)
    text, decision = router.local_first_generate("normal", "quick_text", messages, ledger)
    assert text == "local answer" and decision.provider == "local" and decision.finish == "stop" and "local model first" in decision.reason
    assert ledger.usage("local")["daily_tokens"] > 0 and called == ["cloud"]

    def broken(*a, **k):
        raise ProviderError("local HTTP 503: loading model")
        yield  # pragma: no cover

    monkeypatch.setattr(router, "cortex_stream", broken)
    text, decision = router.local_first_generate("normal", "quick_text", messages, ledger)
    assert text == "cloud answer" and called == ["cloud", "cloud"]


def test_heartbeats_keep_live_jobs_and_reap_silent_ones(db):
    jobs.register_handler("hb", lambda ctx: {})
    live = jobs.enqueue(db, "hb", {}, {})
    dead = jobs.enqueue(db, "hb", {}, {})
    vault.claim_job("worker-a", ("hb",))
    vault.claim_job("worker-b", ("hb",))
    with vault._open_database() as connection:
        connection.execute("UPDATE jobs SET claimed_at = ? WHERE id IN (?, ?)", (time.time() - 1000, live, dead))
    assert vault.touch_heartbeat([live]) == 1  # only the ids live threads hold are stamped
    assert vault.reap_stale_heartbeats("silent", older_than_seconds=180) == 1
    assert vault.job_by_id(live)["status"] == "running" and vault.job_by_id(dead)["status"] == "failed"
    assert vault.job_view(vault.job_by_id(dead))["result"] == {"error": "silent"}


def test_runner_without_workers_does_not_reap_and_worker_mode_parses(db):
    jobs.register_handler("keep", lambda ctx: {})
    job_id = jobs.enqueue(db, "keep", {}, {})
    vault.claim_job("other-container", ("keep",))
    with vault._open_database() as connection:
        connection.execute("UPDATE jobs SET created_at = 0, claimed_at = 0 WHERE id = ?", (job_id,))
    jobs.JobRunner(max_workers=0).start()
    assert vault.job_by_id(job_id)["status"] == "running"  # the app beside a worker leaves the worker's rows alone
    jobs.JobRunner(max_workers=0).start(reap=True)
    assert vault.job_by_id(job_id)["status"] == "failed"
    args = jobs.build_arg_parser().parse_args(["--worker", "--workers", "3", "--poll", "0.5", "--no-bootstrap"])
    assert args.worker and args.workers == 3 and args.poll == 0.5 and args.no_bootstrap
    assert jobs.run_worker([]) == 2  # help, not a worker


def test_bootstrap_restores_only_dead_tick_chains_when_keys_exist(db, monkeypatch, no_local):
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from orchestrator.config import bind_session_keys
    bind_session_keys({})
    dead = tick.start_tick("scope-dead", {}, interval_s=900, leisure_cap=1)
    vault.claim_job("old-worker", (tick.KIND_TICK,))
    vault.reap_stale_jobs(jobs.RESTART_NOTE, queued_before=time.time() + 1)
    assert vault.job_by_id(dead)["status"] == "failed"
    stopped = tick.start_tick("scope-stopped", {})
    tick.stop_tick("scope-stopped")
    assert vault.job_by_id(stopped)["status"] == "cancelled"
    assert tick.bootstrap_ticks() == 0  # no keys anywhere: restoring would only fail every call
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env-key")
    assert tick.bootstrap_ticks() == 1
    restored = vault.list_jobs("scope-dead", ("queued",), kind=tick.KIND_TICK)
    assert len(restored) == 1 and vault.job_view(restored[0])["payload"]["interval_s"] == 900.0 and "chained_from" not in vault.job_view(restored[0])["payload"]
    assert not vault.list_jobs("scope-stopped", ("queued",), kind=tick.KIND_TICK)
    assert tick.bootstrap_ticks() == 0  # already running


def test_vm_kit_has_compose_worker_local_model_and_scripts():
    files = {item.path: item for item in dk.generate_kit(dk.KitSpec(target="oracle-vm", domain="Studio.Example.com", local_model="qwen2.5:7b", worker_threads=3))}
    compose = files["docker-compose.yml"].body
    assert "orchestrator.jobs" in compose and '"--workers", "3"' in compose and "CHAT_JOHNSON_JOB_WORKERS: \"0\"" in compose
    assert "ollama/ollama" in compose and "CHAT_JOHNSON_LOCAL_MODEL: qwen2.5:7b" in compose and "caddy:2" in compose and "data:/data" in compose
    caddy = files["Caddyfile"].body
    assert "{$DOMAIN::80} {" in caddy and "basic_auth" in caddy and "reverse_proxy app:8501" in caddy  # TLS from DOMAIN, auth from CADDY_USER/CADDY_HASH
    assert "basic_auth" not in files["Caddyfile.open"].body and "    env_file:" not in compose.split("  worker:")[0]  # the app service never loads .env
    assert "WITH_BROWSER" in files["Dockerfile"].body and "USER app" in files["Dockerfile"].body
    assert "get.docker.com" in files["scripts/vm-bootstrap.sh"].body and "ollama pull \"qwen2.5:7b\"" in files["scripts/vm-update.sh"].body
    assert "vault-backup.db" in files["scripts/vm-backup.sh"].body
    runbook = files["docs/RUNBOOK.md"].body
    assert "vm-update.sh" in runbook and "Always Free" in runbook and "24/7" in runbook
    assert not any(f.level == "error" for f in dk.validate_kit(list(files.values())))
    plain = {item.path: item for item in dk.generate_kit(dk.KitSpec(target="oracle-vm"))}
    assert "{$DOMAIN::80} {" in plain["Caddyfile"].body  # without a domain the site is :80 behind the localhost bind, never a bare :80 on the internet
