"""Sandbox: isolated git worktree staging + syntax guardrails.

`git worktree add` gives pytest a fully separate checkout so experimental
patches can never corrupt the working tree. Falls back to a plain copy for
non-git directories. All generated .py files must pass ast.parse() before
anything is applied or tested.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple


class SandboxError(RuntimeError):
    pass


def _run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=300
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def create_worktree(repo_path: str, staging_root: str) -> Tuple[str, str]:
    """Create an isolated worktree. Returns (sandbox_path, branch_name).

    - git repo  -> `git worktree add -b orchestrator/<ts> <tmp>`
    - otherwise -> full copy into a temp dir (still isolated).
    """
    os.makedirs(staging_root, exist_ok=True)
    branch = f"orchestrator/{time.strftime('%Y%m%d-%H%M%S')}"
    sandbox = tempfile.mkdtemp(prefix="orch-sandbox-", dir=staging_root)

    is_git = os.path.isdir(os.path.join(repo_path, ".git"))
    if is_git:
        code, out = _run(
            ["git", "worktree", "add", "-b", branch, sandbox, "HEAD"], cwd=repo_path
        )
        if code == 0:
            return sandbox, branch
        # dirty/unborn HEAD etc: fall through to copy mode
    shutil.copytree(
        repo_path, sandbox,
        ignore=shutil.ignore_patterns(".git", ".orchestrator", "__pycache__"),
        dirs_exist_ok=True,
    )
    return sandbox, "(copy-mode)"


def commit_sandbox(sandbox_path: str, message: str) -> bool:
    """Commit sandbox changes so a git diff can be produced (best-effort)."""
    is_git = os.path.isdir(os.path.join(sandbox_path, ".git"))
    if not is_git:
        return False
    _run(["git", "add", "-A"], cwd=sandbox_path)
    code, _ = _run(["git", "commit", "-m", message], cwd=sandbox_path)
    return code == 0


def diff_vs_base(sandbox_path: str) -> str:
    """Unified diff of everything changed in the sandbox (copy-mode: all files)."""
    is_git = os.path.isdir(os.path.join(sandbox_path, ".git"))
    if is_git:
        code, out = _run(["git", "diff", "HEAD", "--stat", "--", "."], cwd=sandbox_path)
        code2, patch = _run(["git", "diff", "HEAD", "--", "."], cwd=sandbox_path)
        if code == 0 and code2 == 0:
            return f"{out}\n\n{patch}" if out or patch else "(no changes)"
    return "(copy-mode sandbox: inspect files directly)"


def cleanup_worktree(repo_path: str, sandbox_path: str) -> None:
    if os.path.isdir(os.path.join(repo_path, ".git")):
        _run(["git", "worktree", "remove", "--force", sandbox_path], cwd=repo_path)
    if os.path.isdir(sandbox_path):
        shutil.rmtree(sandbox_path, ignore_errors=True)


def validate_python_files(root: str, changed: Optional[List[str]] = None) -> List[str]:
    """ast.parse guardrail. Returns list of human-readable errors (empty = OK)."""
    errors: List[str] = []
    targets = changed
    if targets is None:
        targets = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS_LIKE]
            for name in filenames:
                if name.endswith(".py"):
                    targets.append(os.path.relpath(os.path.join(dirpath, name), root))
    for rel in targets:
        if not rel.endswith(".py"):
            continue
        full = os.path.join(root, rel)
        if not os.path.exists(full):
            continue  # deleted file, fine
        try:
            with open(full, "r", encoding="utf-8") as f:
                ast.parse(f.read(), filename=rel)
        except SyntaxError as exc:
            errors.append(f"{rel}: line {exc.lineno}: {exc.msg}")
        except (OSError, ValueError) as exc:
            errors.append(f"{rel}: unreadable ({exc})")
    return errors


SKIP_DIRS_LIKE = {".git", "__pycache__", ".venv", "venv", ".orchestrator", "node_modules"}
