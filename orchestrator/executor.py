"""Executor: the closed loop from goal to verified patch.

1. Ingest repo (Gemini-sized context)        2. Decompose into typed steps
3. Route each step to its best free model    4. Apply patches in a git worktree
5. Guardrails: ast.parse + pytest loop       6. Return verified diff + report
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Dict, List, Optional

from . import sandbox
from .config import PROVIDERS, Settings, get_settings
from .decomposer import decompose
from .memory import StepRecord, TaskMemory
from .errors import plain_error
from .patches import (
    changed_files,
    PATCH_INSTRUCTIONS,
    apply_file_blocks,
    apply_unified_diffs,
    diff_paths,
    parse_diff_blocks,
    parse_file_blocks, reject_snippets,
)
from .quota import QuotaLedger
from .repo_ingest import serialize_repo
from .router import pipeline_generate
from .test_loop import repair_loop


def memory_path_for(base_path: str, project_scope: str = "") -> str:
    """The task-memory file for a scope: the base path alone for the CLI, a scope-hashed sibling for the studio."""
    scope = (project_scope or "").strip()
    if not scope:
        return base_path
    root, ext = os.path.splitext(base_path)
    return f"{root}-{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:12]}{ext or '.json'}"


class Orchestrator:
    def __init__(self, settings: Optional[Settings] = None, ledger: Optional[QuotaLedger] = None):
        self.settings = settings or get_settings()
        self.ledger = ledger or QuotaLedger(
            {name: (cfg.rpm_limit, cfg.tpm_limit) for name, cfg in PROVIDERS.items()}
        )
        self.log: List[Dict] = []

    # ---------- pipeline ----------

    def run(self, goal: str, repo_path: Optional[str] = None, project_scope: str = "") -> Dict:
        """Execute a goal end-to-end. Returns a report dict. ``project_scope`` keeps task memory private per visitor."""
        s = self.settings
        memory = TaskMemory(memory_path_for(s.memory_path, project_scope), goal=goal)
        repo_path = os.path.abspath(repo_path) if repo_path else None

        # 1) repo context (only when a repo is attached)
        repo_context, ingest_stats = ("", {"file_count": 0, "est_tokens": 0})
        if repo_path and os.path.isdir(repo_path):
            repo_context, ingest_stats = serialize_repo(repo_path, s.repo_ingest_budget)

        # 2) decompose (an empty repository gets a fixed two-step scaffold plan: no call to plan, no call to analyze nothing)
        if repo_path and ingest_stats.get("file_count", 0) == 0:
            plan = [
                {"id": 1, "title": "Create the project structure", "type": "code_patch",
                 "description": f"Create every folder and file for: {goal}. The repository is empty; emit the complete files.", "targets": []},
                {"id": 2, "title": "Launch and next steps", "type": "quick_text",
                 "description": f"Explain how to run what was created for: {goal}, and list the next three improvements.", "targets": []},
            ]
        else:
            plan = decompose(goal, memory.context_block(), self.ledger, generate_fn=pipeline_generate)
            if ingest_stats.get("file_count", 0) == 0:
                plan = [step for step in plan if step["type"] != "context_load"] or plan
        self._log_event("plan", steps=plan)

        # 3) sandbox
        sandbox_path, branch = ("", "(no-repo)")
        if repo_path:
            sandbox.prune_staging(s.staging_root)  # earlier runs' sandboxes; this one stays until the next sweep
            sandbox_path, branch = sandbox.create_worktree(repo_path, s.staging_root)
        self._log_event("sandbox", path=sandbox_path, branch=branch)

        try:
            for step in plan:
                self._execute_step(step, goal, memory, repo_context, sandbox_path)
                memory.refresh_summary(self._cheap_summarizer)
        finally:
            if sandbox_path:
                self._log_event("sandbox_diff", diff=sandbox.diff_vs_base(sandbox_path, repo_path))

        # 4) verification report
        report = {
            "goal": goal,
            "branch": branch,
            "sandbox": sandbox_path,
            "plan": plan,
            "steps": [
                {"id": s.step_id, "title": s.title, "type": s.task_type, "provider": s.provider, "status": s.status, "note": s.note}
                for s in memory.steps
            ],
            "memory": memory.context_block(),
            "ingest": ingest_stats,
            "ledger": {p: self.ledger.usage(p) for p in self.ledger._limits},
            "failed_steps": sum(1 for s in memory.steps if s.status == "failed"),
            "diff": sandbox.diff_vs_base(sandbox_path, repo_path) if sandbox_path else "",
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
            text, decision = pipeline_generate(task_type, messages, self.ledger, max_tokens=self.settings.max_output_tokens)
        except Exception as exc:
            record.status, record.note, record.provider = "failed", f"provider error: {plain_error(exc)}", "none"
            memory.add_step(record)
            self._log_event("step_failed", step_id=step["id"], error=str(exc))
            return

        record.provider = decision.provider

        if task_type in ("code_patch", "test_fix") and sandbox_path:
            record.note = self._apply_and_verify(text, sandbox_path, goal, memory, decision.provider)
            if record.note.startswith("FAILED") and "no file blocks" in record.note:
                # Keep the head of the answer so the operator can see what came back instead of files.
                record.note += " · answer began: " + text.strip()[:300].replace("\n", " ")
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
        file_blocks, rejected = reject_snippets(sandbox_path, parse_file_blocks(text))
        diffs = parse_diff_blocks(text)
        if not file_blocks and not diffs:
            if rejected:
                return "FAILED: " + "; ".join(rejected)
            return "FAILED: model output contained no file blocks or diffs"

        written = apply_file_blocks(sandbox_path, file_blocks)
        applied, failed = apply_unified_diffs(sandbox_path, diffs) if diffs else ([], [])

        # guardrail: every generated .py must parse (files a diff touched included, whatever the sandbox mode)
        changed = list(dict.fromkeys(written + changed_files(sandbox_path) + diff_paths("\n".join(diffs))))
        errors = sandbox.validate_python_files(sandbox_path, changed)
        if errors:
            return "FAILED: syntax guardrail:\n" + "\n".join(errors)

        passed, rounds, output = repair_loop(
            sandbox_path, goal, self.ledger, memory, self.settings.max_test_rounds
        )
        if not passed:
            # Left uncommitted on purpose: the diff stays reviewable, but nothing red becomes a commit.
            return f"FAILED after {rounds} repair round(s): {output[-800:]}"
        sandbox.commit_sandbox(sandbox_path, f"orchestrator: {goal[:60]} ({provider})")
        note = f"applied {len(written)} file block(s), {len(applied)} diff(s)"
        if failed:
            note += f"; {len(failed)} diff failed: {failed[0][:120]}"
        if rejected:
            note += f"; skipped {len(rejected)} snippet block(s): {rejected[0][:120]}"
        return note + (f"; pytest green after {rounds} round(s)" if rounds or "not run" not in output else "; tests not run")

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
        elif repo_context and step["type"] in ("code_patch", "test_fix"):
            # Patch steps see the file map and the highest-value files, bounded, so edits target real paths.
            parts.append("\nREPOSITORY (file map, then key files; other files exist but are not shown):\n" + repo_context[:20_000])
        elif not repo_context and step["type"] in ("code_patch", "test_fix"):
            parts.append("\nThe repository is EMPTY: create the complete project structure as file blocks (every folder needs a file).")
        return "\n".join(parts)

    def _cheap_summarizer(self, text: str) -> str:
        out, _ = pipeline_generate(
            "quick_text",
            [{"role": "user", "content": text}],
            self.ledger, max_tokens=256, temperature=0.1,
        )
        return out

    def _log_event(self, event: str, **data) -> None:
        self.log.append({"ts": time.time(), "event": event, **data})
