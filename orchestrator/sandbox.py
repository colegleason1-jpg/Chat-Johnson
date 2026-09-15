"""Sandbox: isolated git worktree staging + syntax guardrails.

`git worktree add` gives pytest a fully separate checkout so experimental
patches can never corrupt the working tree. Falls back to a plain copy for
non-git directories. All generated .py files must pass ast.parse() before
anything is applied or tested.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple

from .patches import GIT_SAFE, is_git_sandbox


class SandboxError(RuntimeError):
    pass


def _run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    if cmd and cmd[0] == "git":
        cmd = ["git", *GIT_SAFE, *cmd[1:]]  # never run hooks, fsmonitor, or external tools from the tree
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=300
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _sidecar(sandbox: str) -> str:
    """The staging-root file that remembers which repository and branch a worktree sandbox belongs to."""
    return sandbox.rstrip("/") + ".source.json"


def sandbox_source(sandbox: str) -> Optional[dict]:
    try:
        with open(_sidecar(sandbox), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def create_worktree(repo_path: str, staging_root: str) -> Tuple[str, str]:
    """Create an isolated worktree. Returns (sandbox_path, branch_name).

    - git repo  -> `git worktree add -b orchestrator/<sandbox name> <tmp>` (a sidecar file records it for cleanup)
    - otherwise -> full copy into a temp dir (still isolated).
    """
    os.makedirs(staging_root, exist_ok=True)
    sandbox = tempfile.mkdtemp(prefix="orch-sandbox-", dir=staging_root)
    branch = f"orchestrator/{os.path.basename(sandbox)}"  # unique per sandbox: no collision between concurrent runs

    is_git = os.path.isdir(os.path.join(repo_path, ".git"))
    if is_git:
        code, out = _run(
            ["git", "worktree", "add", "-b", branch, sandbox, "HEAD"], cwd=repo_path
        )
        if code == 0:
            with open(_sidecar(sandbox), "w", encoding="utf-8") as handle:
                json.dump({"repo_path": os.path.abspath(repo_path), "branch": branch, "created_at": time.time()}, handle)
            return sandbox, branch
        # dirty/unborn HEAD etc: fall through to copy mode
    shutil.copytree(
        repo_path, sandbox,
        ignore=shutil.ignore_patterns(".git", ".orchestrator", "__pycache__"),
        dirs_exist_ok=True,
    )
    return sandbox, "(copy-mode)"


def commit_sandbox(sandbox_path: str, message: str) -> bool:
    """Commit sandbox changes so a git diff can be produced (best-effort; worktrees count as git)."""
    if not is_git_sandbox(sandbox_path):
        return False
    _run(["git", "add", "-A"], cwd=sandbox_path)
    code, _ = _run(["git", "commit", "-m", message], cwd=sandbox_path)
    return code == 0


def _copy_mode_diff(source_root: str, sandbox_path: str, limit_bytes: int = 200_000) -> str:
    """Unified diff of text files that differ between a copied sandbox and its source tree."""
    import difflib

    chunks: List[str] = []
    total = 0
    for root, dirs, files in os.walk(sandbox_path):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".orchestrator", "node_modules"}]
        for name in files:
            new_path = os.path.join(root, name)
            rel = os.path.relpath(new_path, sandbox_path)
            old_path = os.path.join(source_root, rel)
            try:
                new_text = open(new_path, encoding="utf-8").read()
                old_text = open(old_path, encoding="utf-8").read() if os.path.exists(old_path) else ""
            except (UnicodeDecodeError, OSError):
                continue
            if new_text == old_text:
                continue
            diff = "".join(difflib.unified_diff(
                old_text.splitlines(True), new_text.splitlines(True), fromfile=f"a/{rel}", tofile=f"b/{rel}"
            ))
            total += len(diff)
            chunks.append(diff)
            if total > limit_bytes:
                chunks.append(f"... diff truncated at {limit_bytes} bytes ...\n")
                return "".join(chunks)
    return "".join(chunks) or "(copy-mode sandbox: no text files differ from the source tree)"


def diff_vs_base(sandbox_path: str, source_root: Optional[str] = None) -> str:
    """Unified diff of everything changed in the sandbox (copy-mode: all files)."""
    if is_git_sandbox(sandbox_path):
        source = sandbox_source(sandbox_path)
        base = "HEAD"
        if source:
            # The sandbox commits as it goes, so diff against the branch point, not the moving HEAD.
            code0, merge_base = _run(["git", "merge-base", "HEAD", "HEAD@{upstream}"], cwd=sandbox_path)
            code0, first = _run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=sandbox_path)
            code0, forked = _run(["git", "log", "--format=%H", "-n", "1", "--grep=^orchestrator:", "--invert-grep", "HEAD"], cwd=sandbox_path)
            base = forked.splitlines()[0].strip() if code0 == 0 and forked.strip() else "HEAD"
        code, out = _run(["git", "diff", base, "--stat", "--", "."], cwd=sandbox_path)
        code2, patch = _run(["git", "diff", base, "--", "."], cwd=sandbox_path)
        if code == 0 and code2 == 0:
            return f"{out}\n\n{patch}" if out or patch else "(no changes)"
    if source_root and os.path.isdir(source_root):
        return _copy_mode_diff(source_root, sandbox_path)
    return "(copy-mode sandbox: inspect files directly)"


def cleanup_worktree(repo_path: str, sandbox_path: str, branch: Optional[str] = None) -> None:
    """Remove a sandbox and, for a worktree, its registration and branch in the source repository."""
    source = sandbox_source(sandbox_path) or {}
    repo = source.get("repo_path") or repo_path
    branch = branch or source.get("branch")
    if os.path.isdir(os.path.join(repo, ".git")):
        _run(["git", "worktree", "remove", "--force", sandbox_path], cwd=repo)
    if os.path.isdir(sandbox_path):
        shutil.rmtree(sandbox_path, ignore_errors=True)
    if os.path.isdir(os.path.join(repo, ".git")):
        _run(["git", "worktree", "prune"], cwd=repo)
        if branch and branch != "(copy-mode)":
            _run(["git", "branch", "-D", branch], cwd=repo)
    try:
        os.remove(_sidecar(sandbox_path))
    except OSError:
        pass


def prune_staging(staging_root: str, max_age_seconds: int = 2 * 3600) -> int:
    """Remove sandboxes older than ``max_age_seconds``; returns how many were removed.

    A finished run keeps its sandbox so the push flow can read the changed files; this sweep
    (run start and job-runner start) is what reclaims the disk. A worktree sandbox's sidecar
    names its source repository, so its registration and branch are removed with it.
    """
    if not os.path.isdir(staging_root):
        return 0
    removed = 0
    now = time.time()
    for name in os.listdir(staging_root):
        path = os.path.join(staging_root, name)
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > max_age_seconds:
                cleanup_worktree("", path)
                removed += 1
            elif name.endswith(".source.json") and not os.path.isdir(path[: -len(".source.json")]):
                os.remove(path)  # a sidecar whose sandbox is already gone
        except OSError:
            continue
    return removed


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
