"""Background job runner: missions, society cycles, and sub-agents run off the script thread.

Rows live in the vault's ``jobs`` table so any process can claim them; this module adds an
in-process pool of daemon threads for hosts without a separate worker container. A job can
ask the operator a question (``waiting_input``) without stopping other work, and cancel is
cooperative: handlers call ``check_cancel`` between steps.

Secrets never touch the ``jobs`` row. ``enqueue`` hands them over one of two ways: encrypted
(``jobsecrets``, when ``CHAT_JOHNSON_JOB_KEY`` is set) as a blob the claiming worker deletes on
claim, or in a process dict keyed by job id that ages out after an hour (only a worker thread in
this same process can take it). Either way the worker binds them as the session-key overlay of a
fresh ``contextvars.Context`` for that job only and drops them when the job ends. A process
without workers and without the key cannot deliver secrets to anyone; ``secrets_deliverable``
says so and the UI refuses nodes that need them.
"""
from __future__ import annotations

import contextvars
import json
import os
import secrets as _secrets
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Set, Tuple

from . import jobsecrets, vault
from .config import bind_session_keys
from .quota import QuotaLedger
from .quota_registry import get_quota_ledger, get_request_lock

ACTIVE_STATUSES: Tuple[str, ...] = ("queued", "running", "waiting_input")
FINISHED_STATUSES: Tuple[str, ...] = ("done", "failed", "cancelled")
DEFAULT_WORKERS = 2
ANSWER_TIMEOUT_SECONDS = 900.0
_PROCESS_STARTED = time.time()
RESTART_NOTE = "the app restarted before this job finished (its session keys are gone); launch it again"
STALE_NOTE = "the worker running this job stopped responding; launch it again"
HEARTBEAT_SECONDS = 15.0
STALE_AFTER_SECONDS = 180.0
SECRET_TTL_SECONDS = 3600.0


class JobCancelled(Exception):
    """Raised inside a handler when the operator pressed Cancel."""


class JobAnswerTimeout(Exception):
    """Raised when a question to the operator went unanswered for too long."""


@dataclass
class JobContext:
    """What a handler gets: its row's payload, the secrets for this job, and the shared quota objects."""

    job_id: int
    project_scope: str
    thread_id: Optional[int]
    kind: str
    payload: Dict[str, Any]
    secrets: Dict[str, str]
    ledger: QuotaLedger
    request_lock: threading.Lock

    def progress(self, **fields: Any) -> None:
        vault.update_job_progress(self.job_id, fields)

    def check_cancel(self) -> None:
        if vault.cancel_requested(self.job_id):
            raise JobCancelled()

    def sleep(self, seconds: float) -> None:
        """Sleep in one-second slices so a cancel lands promptly."""
        deadline = time.monotonic() + float(seconds)
        while True:
            self.check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def ask(self, question: str, timeout: float = ANSWER_TIMEOUT_SECONDS) -> str:
        """Park the job as waiting_input until the operator answers in the jobs strip (or cancels)."""
        vault.ask_job_question(self.job_id, question)
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            self.check_cancel()
            answer = vault.job_answer(self.job_id)
            if answer is not None:
                return answer
            time.sleep(0.5)
        raise JobAnswerTimeout(f"no answer within {int(timeout)}s: {question[:80]}")


Handler = Callable[[JobContext], Mapping[str, Any]]
_HANDLERS: Dict[str, Handler] = {}
_SECRETS: Dict[int, Tuple[float, Dict[str, str]]] = {}
_SECRETS_LOCK = threading.Lock()


def register_handler(kind: str, handler: Handler) -> None:
    _HANDLERS[kind] = handler


def handler_kinds() -> Tuple[str, ...]:
    return tuple(_HANDLERS)


def local_workers() -> int:
    """Worker threads this process runs (the runner's count once started, else the environment's)."""
    if _RUNNER is not None:
        return _RUNNER.max_workers
    value = os.environ.get("CHAT_JOHNSON_JOB_WORKERS", "")
    return int(value) if value.strip().isdigit() else DEFAULT_WORKERS


def secrets_deliverable() -> bool:
    """Can a job enqueued here receive session secrets? Encrypted hand-off, or workers in this process."""
    return jobsecrets.available() or local_workers() > 0


def enqueue(
    project_scope: str, kind: str, payload: Mapping[str, Any], secrets: Mapping[str, str], thread_id: Optional[int] = None,
    run_after: float = 0.0,
) -> int:
    """Queue a job; ``secrets`` (env-name → key) travel encrypted or in memory, never in the row.

    ``run_after`` delays the claim (epoch seconds): chained cycles use it as their scheduler.
    """
    if kind not in _HANDLERS:
        raise KeyError(f"no handler registered for job kind {kind!r}")
    job_id = vault.enqueue_job(project_scope, kind, payload, thread_id=thread_id, run_after=run_after)
    clean = {name: value for name, value in secrets.items() if value}
    if not clean:
        return job_id
    blob = jobsecrets.encrypt(clean)
    if blob is not None:
        vault.store_job_secrets(job_id, blob)
    else:
        with _SECRETS_LOCK:
            _purge_secrets_locked()  # bounded: entries older than SECRET_TTL_SECONDS leave on every enqueue
            _SECRETS[job_id] = (time.time(), clean)
    return job_id


def _purge_secrets_locked() -> None:
    cutoff = time.time() - SECRET_TTL_SECONDS
    for job_id in [job_id for job_id, (stamp, _) in _SECRETS.items() if stamp < cutoff]:
        _SECRETS.pop(job_id, None)


def secrets_held(job_id: int) -> bool:
    with _SECRETS_LOCK:
        if job_id in _SECRETS:
            return True
    return vault.has_job_secrets(job_id)


def _take_secrets(job_id: int) -> Dict[str, str]:
    with _SECRETS_LOCK:
        entry = _SECRETS.pop(job_id, None)
    if entry is not None:
        return dict(entry[1])
    return jobsecrets.decrypt(vault.take_job_secrets(job_id))


def run_job(row: Any) -> None:
    """Execute one claimed row to completion in the calling thread, inside a fresh key context."""
    contextvars.Context().run(_run_in_context, row)


def _finish(job_id: int, status: str, result: Mapping[str, Any]) -> None:
    """Finish the row; one retry when SQLite is busy so a locked database never strands a job as running."""
    try:
        vault.finish_job(job_id, status, result)
    except sqlite3.OperationalError:
        time.sleep(1.0)
        vault.finish_job(job_id, status, result)


def _run_in_context(row: Any) -> None:
    job_id = int(row["id"])
    kind = str(row["kind"])
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    secrets = _take_secrets(job_id)
    bind_session_keys(secrets)
    ctx = JobContext(
        job_id=job_id, project_scope=str(row["project_scope"]), thread_id=row["thread_id"], kind=kind,
        payload=payload, secrets=secrets, ledger=get_quota_ledger(), request_lock=get_request_lock(),
    )
    try:
        handler = _HANDLERS.get(kind)
        if handler is None:
            raise RuntimeError(f"no handler registered for job kind {kind!r}")
        ctx.check_cancel()
        result = handler(ctx)
        _finish(job_id, "done", dict(result or {}))
    except JobCancelled:
        _finish(job_id, "cancelled", {"note": "cancelled by the operator"})
    except Exception as exc:  # the row carries the error; the worker thread must survive
        _finish(job_id, "failed", {"error": str(exc)[:600], "type": type(exc).__name__})
    finally:
        secrets.clear()
        bind_session_keys({})


class JobRunner:
    """Daemon worker threads that claim queued rows and run their handlers."""

    def __init__(self, max_workers: Optional[int] = None, poll_seconds: float = 1.0) -> None:
        env_value = os.environ.get("CHAT_JOHNSON_JOB_WORKERS", "")
        if max_workers is None:
            max_workers = int(env_value) if env_value.strip().isdigit() else DEFAULT_WORKERS
        self.max_workers = max(0, int(max_workers))
        self.poll_seconds = float(poll_seconds)
        self.host = socket.gethostname()
        # A restarted container keeps its hostname and often its pid, so the name carries a random suffix.
        self.worker_name = f"{self.host}:{os.getpid()}:{_secrets.token_hex(3)}"
        self._stop = threading.Event()
        self._threads: list = []
        self._active: Set[int] = set()
        self._active_lock = threading.Lock()

    def active_jobs(self) -> Tuple[int, ...]:
        with self._active_lock:
            return tuple(sorted(self._active))

    def start(self, reap: Optional[bool] = None) -> "JobRunner":
        """Start the workers. With no workers (the app beside a worker container) nothing is reaped: the worker owns the rows."""
        if reap if reap is not None else self.max_workers > 0:
            vault.reap_stale_jobs(RESTART_NOTE, queued_before=_PROCESS_STARTED)
        _sweep_staging()
        for index in range(self.max_workers):
            thread = threading.Thread(target=self._loop, name=f"job-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        if self.max_workers > 0:
            keeper = threading.Thread(target=self._housekeeping, name="job-housekeeping", daemon=True)
            keeper.start()
            self._threads.append(keeper)
        return self

    def _housekeeping(self) -> None:
        """Stamp the rows live threads hold; fail rows whose worker went silent; restore ticks a dead worker dropped."""
        last_reap = time.monotonic()
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                vault.touch_heartbeat(self.active_jobs())
                if time.monotonic() - last_reap >= 60.0:
                    reaped = vault.reap_stale_heartbeats(STALE_NOTE, STALE_AFTER_SECONDS)
                    vault.purge_job_secrets(SECRET_TTL_SECONDS)
                    with _SECRETS_LOCK:
                        _purge_secrets_locked()
                    last_reap = time.monotonic()
                    if reaped:
                        _restore_ticks()
            except Exception:  # housekeeping must never take a worker down
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            row = None
            try:
                row = vault.claim_job(self.worker_name, handler_kinds())
            except Exception:  # a locked or missing database is retried on the next poll
                row = None
            if row is None:
                self._stop.wait(self.poll_seconds)
                continue
            job_id = int(row["id"])
            with self._active_lock:
                self._active.add(job_id)
            try:
                run_job(row)
            except Exception:  # a failure while finishing the row must not kill the pool
                self._stop.wait(2.0)
            finally:
                with self._active_lock:
                    self._active.discard(job_id)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout)

    @property
    def alive(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())


def _restore_ticks() -> None:
    """After a reap, re-queue society ticks whose chain died with the reaped worker."""
    try:
        from . import society

        society.bootstrap_ticks()
    except Exception:
        pass


def _sweep_staging() -> None:
    """Drop stale repository sandboxes and fetched trees; nothing else calls the two prune helpers."""
    try:
        from . import sandbox
        from .config import Settings
        from .github_repo import prune_staging

        sandbox.prune_staging(Settings().staging_root)
        prune_staging()
    except Exception:  # housekeeping must never block start-up
        pass


_RUNNER: Optional[JobRunner] = None
_RUNNER_LOCK = threading.Lock()


def get_runner() -> JobRunner:
    """The process singleton, started on first use (``CHAT_JOHNSON_JOB_WORKERS=0`` keeps it idle)."""
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = JobRunner().start()
        return _RUNNER


def job_summary_rows(project_scope: str, statuses: Optional[Sequence[str]] = None, limit: int = 20) -> list:
    """Parsed job rows for display."""
    return [vault.job_view(row) for row in vault.list_jobs(project_scope, statuses, limit=limit)]


# =============================================================================
# Worker mode: `python -m orchestrator.jobs --worker` runs the pool in the foreground
# =============================================================================

def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="python -m orchestrator.jobs", description="Chat Johnson background worker")
    parser.add_argument("--worker", action="store_true", help="run the job workers in the foreground until SIGTERM")
    parser.add_argument("--workers", type=int, default=None, help="worker threads (default CHAT_JOHNSON_JOB_WORKERS or 2)")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between queue polls")
    parser.add_argument("--no-bootstrap", action="store_true", help="do not re-queue society ticks that were running before a restart")
    return parser


def run_worker(argv: Optional[Sequence[str]] = None) -> int:
    """Foreground worker for a container: registers every handler, restores ticks, serves the queue until stopped."""
    import logging
    import signal

    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("chat_johnson.worker")
    if not args.worker:
        build_arg_parser().print_help()
        return 2
    from . import mission_runner, society  # noqa: F401  (register the mission, company, academy, and tick handlers)
    from .router import register_local_endpoint_from_env

    vault.initialize_database()
    local = register_local_endpoint_from_env()
    if local is not None:
        log.info("local model endpoint: %s (%s)", local.base_url, local.model)
    runner = JobRunner(max_workers=args.workers, poll_seconds=args.poll)
    # Rows an earlier process of this container left running can never finish: fail them before serving.
    orphaned = vault.fail_jobs_of_host(f"{runner.host}:", STALE_NOTE, except_worker=runner.worker_name)
    runner.start(reap=False)
    reaped = vault.reap_stale_heartbeats(STALE_NOTE, STALE_AFTER_SECONDS)
    if orphaned or reaped:
        log.info("failed %d orphaned and %d stale job row(s) from before this start", orphaned, reaped)
    if not jobsecrets.available():
        log.warning("%s is not set: session secrets (GitHub token, paid slot) cannot reach this worker", jobsecrets.KEY_ENV)
    if not args.no_bootstrap:
        restored = society.bootstrap_ticks()
        log.info("society ticks restored: %d", restored)
    log.info("worker %s serving kinds %s with %d thread(s)", runner.worker_name, ", ".join(handler_kinds()), runner.max_workers)
    stop = threading.Event()

    def _signal(signum, frame):  # noqa: ARG001
        log.info("signal %s: stopping", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _signal)
    while not stop.wait(1.0):
        pass
    runner.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the container
    raise SystemExit(run_worker())
