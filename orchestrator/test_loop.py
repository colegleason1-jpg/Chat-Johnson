"""Automated test + repair loop.

Run pytest in the sandbox; on failure feed the exact traceback back to a
reasoning-capable free model and let it emit a corrected patch, until tests
pass or max rounds are exhausted.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Tuple

from .memory import TaskMemory
from .patches import PATCH_INSTRUCTIONS, apply_file_blocks, parse_file_blocks
from .quota import QuotaLedger
from .router import generate
from .sandbox import validate_python_files


def run_pytest(sandbox_path: str, timeout: int = 300) -> Tuple[bool, str]:
    """Returns (passed, combined_output)."""
    if not os.path.isdir(os.path.join(sandbox_path, "tests")) and not _has_tests(sandbox_path):
        return True, "(no tests found — syntax guardrails only)"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=sandbox_path, capture_output=True, text=True, timeout=timeout,
    )
    output = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, output[-8000:]  # keep tail (tracebacks live there)


def _has_tests(root: str) -> bool:
    for name in ("test_", "conftest.py"):
        try:
            hits = any(f.startswith(name) or f == name for f in os.listdir(root))
        except OSError:
            return False
        if hits:
            return True
    return any(
        os.path.isdir(os.path.join(root, d)) and
        any(f.startswith("test_") for f in os.listdir(os.path.join(root, d)))
        for d in ("tests", "test")
    )


PROMPT_FIX = (
    "The following pytest run failed. Fix the code by emitting corrected FILE blocks.\n"
    + PATCH_INSTRUCTIONS
    + "\nGOAL:\n{goal}\n\nPYTEST OUTPUT:\n{output}\n\n"
    "Fix ONLY what is needed to make the failing tests pass without weakening them."
)


def repair_loop(
    sandbox_path: str,
    goal: str,
    ledger: QuotaLedger,
    memory: TaskMemory,
    max_rounds: int = 3,
) -> Tuple[bool, int, str]:
    """Run tests; while failing, feed tracebacks to the router for fixes.

    Returns (all_passed, rounds_used, final_output).
    """
    passed, output = run_pytest(sandbox_path)
    rounds = 0
    while not passed and rounds < max_rounds:
        rounds += 1
        syntax_errors = validate_python_files(sandbox_path)
        prompt = PROMPT_FIX.format(goal=goal, output=syntax_errors and
                                   "SYNTAX ERRORS:\n" + "\n".join(syntax_errors) + "\n\n" + output
                                   or output)
        try:
            text, _ = generate(
                "test_fix",
                [
                    {"role": "system", "content": "You are a precise Python repair agent. Output only file blocks."},
                    {"role": "user", "content": prompt},
                ],
                ledger,
                max_tokens=8192,
            )
        except Exception as exc:  # provider outage: stop the loop, report
            return False, rounds, f"{output}\n\n[repair loop aborted: {exc}]"
        fixes = parse_file_blocks(text)
        if not fixes:
            output = f"{output}\n\n[round {rounds}: model produced no parseable file blocks]"
            continue
        apply_file_blocks(sandbox_path, fixes)
        passed, output = run_pytest(sandbox_path)
    return passed, rounds, output
