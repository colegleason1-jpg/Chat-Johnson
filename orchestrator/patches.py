"""Deterministic patch assembly: never trust a model with whole-repo rewrites.

The model is instructed to emit one of:
1. FILE blocks (preferred for targeted edits):

       path/relative/to/root
       <complete new file content>

2. A unified diff inside a diff-fenced code block (applied with git apply).

Parsing is fully deterministic; anything unrecognized is rejected so the
feedback loop can ask the model to reformat.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Tuple

FILE_BLOCK_RE = re.compile(
    r"```file:\s*(?P<path>[^\n`]+)\n(?P<body>.*?)```", re.DOTALL
)
DIFF_BLOCK_RE = re.compile(r"```diff\s*\n(?P<body>.*?)```", re.DOTALL)

PATCH_INSTRUCTIONS = """OUTPUT FORMAT (mandatory):
- To change a file, emit a fenced block exactly like:
```file: relative/path/from/repo/root.py
<complete new content of that file>
```
- Only emit files you actually change. Never use placeholders like
  "... rest unchanged ..." — always write the complete file content.
- Prefer many small file blocks over one giant rewrite.
"""


def parse_file_blocks(text: str) -> Dict[str, str]:
    """Extract {relative_path: full_new_content} from FILE blocks."""
    patches: Dict[str, str] = {}
    for m in FILE_BLOCK_RE.finditer(text):
        path = m.group("path").strip()
        body = m.group("body")
        if body.startswith("\n"):
            body = body[1:]
        if not body.endswith("\n"):
            body += "\n"
        if _safe_relpath(path):
            patches[path] = body
    return patches


def parse_diff_blocks(text: str) -> List[str]:
    return [m.group("body") for m in DIFF_BLOCK_RE.finditer(text)]


def _safe_relpath(path: str) -> bool:
    """Reject absolute paths and traversal outside the sandbox."""
    if not path or path.startswith(("/", "~")) or ".." in path.split("/"):
        return False
    drive = os.path.splitdrive(path)[0]
    return not drive


def apply_file_blocks(root: str, patches: Dict[str, str]) -> List[str]:
    """Write files deterministically. Returns list of written relative paths."""
    written: List[str] = []
    for rel, content in patches.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(rel)
    return written


def apply_unified_diffs(root: str, diffs: List[str]) -> Tuple[List[str], List[str]]:
    """Apply diff blocks with `git apply`. Returns (applied, failed_msgs)."""
    applied: List[str] = []
    failed: List[str] = []
    for i, diff in enumerate(diffs):
        proc = subprocess.run(
            ["git", "apply", "--whitespace=fix", "-"],
            cwd=root, input=diff, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            applied.append(f"diff[{i}]")
        else:
            failed.append(f"diff[{i}]: {proc.stderr.strip()[:300]}")
    return applied, failed


def changed_files(root: str) -> List[str]:
    """All files modified vs HEAD (git) — used to scope ast validation."""
    if not os.path.isdir(os.path.join(root, ".git")):
        return []
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    out = []
    for line in proc.stdout.splitlines():
        if len(line) > 3 and line[2] != " ":
            rel = line[3:].strip().strip('"')
            out.append(rel)
    return out
