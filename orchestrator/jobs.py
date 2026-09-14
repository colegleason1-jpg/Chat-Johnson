"""Background job runner: missions (later pipelines and sub-agents) run off the script thread.

Rows live in the vault's ``jobs`` table so any process can claim them; this module adds an
in-process pool of daemon threads for hosts without a separate worker container. A job can
ask the operator a question (``waiting_input``) without stopping other work, and cancel is
cooperative: handlers call ``check_cancel`` between steps.

Secrets never touch the table. ``enqueue`` parks them in a process dict keyed by job id; the
worker pops them when it claims the job, binds them as the session-key overlay of a fresh
``contextvars.Context`` for that job only, and drops them when the job ends. A process restart
therefore loses them, which is why ``reap_stale_jobs`` fails every unfinished row on start.
"""
from __future__ import annotations

import contextvars
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from . import vault
from .config import bind_session_keys
from .quota import QuotaLedger
from .quota_registry import get_quota_ledger, get_request_lock

ACTIVE_STATUSES: Tuple[str, ...] = ("queued", "running", "waiting_input")
FINISHED_STATUSES: Tuple[str, ...] = ("done", "failed", "cancelled")
DEFAULT_WORKERS = 2
ANSWER_TIMEOUT_SECONDS = 900.0
_PROCESS_STARTED = time.time()
RESTART_NOTE = "the app restarted before this job finished (its session keys are gone); launch it again"


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
_SECRETS: Dict[int, Dict[str, str]] = {}
_SECRETS_LOCK = threading.Lock()


def register_handler(kind: str, handler: Handler) -> None:
    _HANDLERS[kind] = handler


def handler_kinds() -> Tuple[str, ...]:
    return tuple(_HANDLERS)


def enqueue(project_scope: str, kind: str, payload: Mapping[str, Any], secrets: Mapping[str, str], thread_id: Optional[int] = None) -> int:
    """Queue a job; ``secrets`` (env-name → key) stay in memory and are bound for that job's context only."""
    if kind not in _HANDLERS:
        raise KeyError(f"no handler registered for job kind {kind!r}")
    job_id = vault.enqueue_job(project_scope, kind, payload, thread_id=thread_id)
    with _SECRETS_LOCK:
        _SECRETS[job_id] = {name: value for name, value in secrets.items() if value}
    return job_id


def secrets_held(job_id: int) -> bool:
    with _SECRETS_LOCK:
        return job_id in _SECRETS


def _take_secrets(job_id: int) -> Dict[str, str]:
    with _SECRETS_LOCK:
        return _SECRETS.pop(job_id, {})


def run_job(row: Any) -> None:
    """Execute one claimed row to completion in the calling thread, inside a fresh key context."""
    contextvars.Context().run(_run_in_context, row)


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
        vault.finish_job(job_id, "done", dict(result or {}))
    except JobCancelled:
        vault.finish_job(job_id, "cancelled", {"note": "cancelled by the operator"})
    except Exception as exc:  # the row carries the error; the worker thread must survive
        vault.finish_job(job_id, "failed", {"error": str(exc)[:600], "type": type(exc).__name__})
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
        self.worker_name = f"{socket.gethostname()}:{os.getpid()}"
        self._stop = threading.Event()
        self._threads: list = []

    def start(self) -> "JobRunner":
        vault.reap_stale_jobs(RESTART_NOTE, queued_before=_PROCESS_STARTED)
        _sweep_staging()
        for index in range(self.max_workers):
            thread = threading.Thread(target=self._loop, name=f"job-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

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
            run_job(row)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout)

    @property
    def alive(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())


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
