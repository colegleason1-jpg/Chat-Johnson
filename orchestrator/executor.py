"""Executor: the closed loop from goal to verified patch.

1. Ingest repo (Gemini-sized context)        2. Decompose into typed steps
3. Route each step to its best free model    4. Apply patches in a git worktree
5. Guardrails: ast.parse + pytest loop       6. Return verified diff + report
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from . import sandbox
from .config import PROVIDERS, Settings, get_settings
from .decomposer import decompose
from .memory import StepRecord, TaskMemory
from .patches import (
    PATCH_INSTRUCTIONS,
    apply_file_blocks,
    apply_unified_diffs,
    parse_diff_blocks,
    parse_file_blocks,
)
from .quota import QuotaLedger
from .repo_ingest import serialize_repo
from .router import generate
from .test_loop import repair_loop


class Orchestrator:
    def __init__(self, settings: Optional[Settings] = None, ledger: Optional[QuotaLedger] = None):
        self.settings = settings or get_settings()
        self.ledger = ledger or QuotaLedger(
            {name: (cfg.rpm_limit, cfg.tpm_limit) for name, cfg in PROVIDERS.items()}
        )
        self.log: List[Dict] = []

    # ---------- pipeline ----------

    def run(self, goal: str, repo_path: Optional[str] = None) -> Dict:
        """Execute a goal end-to-end. Returns a report dict."""
        s = self.settings
        memory = TaskMemory(os.path.join(s.memory_path), goal=goal)
        repo_path = os.path.abspath(repo_path) if repo_path else None

        # 1) repo context (only when a repo is attached)
        repo_context, ingest_stats = ("", {"file_count": 0, "est_tokens": 0})
        if repo_path and os.path.isdir(repo_path):
            repo_context, ingest_stats = serialize_repo(repo_path, s.repo_ingest_budget)

        # 2) decompose
        plan = decompose(goal, memory.context_block(), self.ledger)
        self._log_event("plan", steps=plan)

        # 3) sandbox
        sandbox_path, branch = ("", "(no-repo)")
        if repo_path:
            sandbox_path, branch = sandbox.create_worktree(repo_path, s.staging_root)
        self._log_event("sandbox", path=sandbox_path, branch=branch)

        try:
            for step in plan:
                self._execute_step(step, goal, memory, repo_context, sandbox_path)
                memory.refresh_summary(self._cheap_summarizer)
        finally:
            if sandbox_path:
                self._log_event("sandbox_diff", diff=sandbox.diff_vs_base(sandbox_path))

        # 4) verification report
        report = {
            "goal": goal,
            "branch": branch,
            "sandbox": sandbox_path,
            "steps": [s.__dict__ if hasattr(s, "__dict__") else s for s in plan],
            "memory": memory.context_block(),
            "ingest": ingest_stats,
            "ledger": {p: self.ledger.usage(p) for p in self.ledger._limits},
            "diff": sandbox.diff_vs_base(sandbox_path) if sandbox_path else "",
        }
        return report

    # ---------- step execution ----------

    def _execute_step(
        self, step: Dict, goal: str, memory: TaskMemory, repo_context: str, sandbox_path: str
    ) -> None:
        task_type = step["type"]
        record = StepRecord(
            step_id=step["id"], title=step["title"], task_type=task_type,
            provider="(pending)", status="pending",
        )
        memory.add_step(record)

        messages = [
            {"role": "system", "content": self._system_prompt(task_type)},
            {"role": "user", "content": self._user_prompt(step, goal, memory, repo_context)},
        ]
        try:
            text, decision = generate(task_type, messages, self.ledger, max_tokens=8192)
        except Exception as exc:
            record.status, record.note = "failed", f"provider error: {exc}"
            memory.add_step(record)
            self._log_event("step_failed", step_id=step["id"], error=str(exc))
            return

        record.provider = decision.provider

        if task_type in ("code_patch", "test_fix") and sandbox_path:
            record.note = self._apply_and_verify(text, sandbox_path, goal, memory, decision.provider)
            record.status = "done" if not record.note.startswith("FAILED") else "failed"
        else:
            record.note = text[:400].replace("\n", " ")
            record.status = "done"

        memory.add_step(record)
        self._log_event("step_done", step_id=step["id"], provider=decision.provider,
                        model=decision.model, status=record.status)

    def _apply_and_verify(
        self, text: str, sandbox_path: str, goal: str, memory: TaskMemory, provider: str
    ) -> str:
        """Deterministic assembly + guardrails + pytest repair loop."""
        file_blocks = parse_file_blocks(text)
        diffs = parse_diff_blocks(text)
        if not file_blocks and not diffs:
            return "FAILED: model output contained no file blocks or diffs"

        written = apply_file_blocks(sandbox_path, file_blocks)
        applied, failed = apply_unified_diffs(sandbox_path, diffs) if diffs else ([], [])

        # guardrail: every generated .py must parse
        changed = written + sandbox.changed_files(sandbox_path)
        errors = sandbox.validate_python_files(sandbox_path, changed)
        if errors:
            return "FAILED: syntax guardrail:\n" + "\n".join(errors)

        passed, rounds, output = repair_loop(
            sandbox_path, goal, self.ledger, memory, self.settings.max_test_rounds
        )
        sandbox.commit_sandbox(sandbox_path, f"orchestrator: {goal[:60]} ({provider})")
        if not passed:
            return f"FAILED after {rounds} repair round(s): {output[-800:]}"
        note = f"applied {len(written)} file block(s), {len(applied)} diff(s)"
        if failed:
            note += f"; {len(failed)} diff failed: {failed[0][:120]}"
        return note + f"; pytest green after {rounds} round(s)"

    # ---------- prompt helpers ----------

    def _system_prompt(self, task_type: str) -> str:
        base = "You are part of a free-tier orchestration pipeline. Be precise and complete."
        if task_type in ("code_patch", "test_fix"):
            return base + "\n" + PATCH_INSTRUCTIONS
        if task_type == "context_load":
            return base + " Summarize structure, key modules, and state handlers."
        return base

    def _user_prompt(self, step: Dict, goal: str, memory: TaskMemory, repo_context: str) -> str:
        parts = [f"OVERALL GOAL:\n{goal}", f"\nCURRENT STEP (type={step['type']}):\n{step['description']}"]
        if step.get("targets"):
            parts.append("TARGET FILES: " + ", ".join(step["targets"]))
        mem = memory.context_block()
        if mem:
            parts.append("\nMEMORY:\n" + mem)
        if repo_context and step["type"] == "context_load":
            parts.append("\n" + repo_context[: self.settings.repo_ingest_budget * 4])
        elif repo_context and step.get("targets"):
            parts.append("\nRelevant repo files may already be summarized in MEMORY.")
        return "\n".join(parts)

    def _cheap_summarizer(self, text: str) -> str:
        out, _ = generate(
            "quick_text",
            [{"role": "user", "content": text}],
            self.ledger, max_tokens=256, temperature=0.1,
        )
        return out

    def _log_event(self, event: str, **data) -> None:
        self.log.append({"ts": time.time(), "event": event, **data})
